"""Long-running automatic observation runner.

The runner is orchestration-only.  It delegates temporal alignment to the
observation coordinator and delegates production/research state changes to the
existing review and missed-opportunity paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .missed_opportunity import DEFAULT_TRACKER_PATH
from .missed_opportunity_live import apply_missed_opportunity_observation
from .model import ACTIVE_SYMBOLS, TIMEFRAMES, MarketDataFrame, to_market_data_frame
from .observation_coordinator import BLOCKED, READY, WAITING_FOR_M5_CLOSE, prepare_observation
from .opportunity import extract_opportunity_snapshot
from .persistence import atomic_write_json, load_review_state
from .pipeline import load_snapshot, review_snapshot


RUNNER_SCHEMA = "observation-runner.v1"
RUNNER_ACTIVE_STATUSES = {"STARTING", "IDLE", "PREPARING", "WAITING_FOR_M5_CLOSE", "REVIEWING", "PERSISTING", "BLOCKED"}
RUNNER_ALREADY_ACTIVE = "RUNNER_ALREADY_ACTIVE"
DEFAULT_WEEKDAY_INTERVAL_MINUTES = 60
DEFAULT_WEEKEND_INTERVAL_MINUTES = 90
WAITING_SAFETY_DELAY_SECONDS = 10

Clock = Callable[[], int]
Sleeper = Callable[[float], None]
Coordinator = Callable[..., dict[str, Any]]
ProductionExecutor = Callable[[dict[str, Any], int], dict[str, Any]]
ResearchExecutor = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass(frozen=True)
class RunnerConfig:
    output_dir: Path = Path("artifact")
    state_path: Path = Path("reviews/thesis-baseline.json")
    research_tracker_path: Path = DEFAULT_TRACKER_PATH
    liquidation_root: Path = Path("artifact/liquidations")
    weekday_interval_minutes: int = DEFAULT_WEEKDAY_INTERVAL_MINUTES
    weekend_interval_minutes: int = DEFAULT_WEEKEND_INTERVAL_MINUTES
    max_cycles: int | None = None
    dry_run: bool = False
    stale_lock_seconds: int = 6 * 60 * 60

    @property
    def runner_state_path(self) -> Path:
        return self.output_dir / "observation-runner.json"

    @property
    def runner_log_path(self) -> Path:
        return self.output_dir / "observation-runner.log"


def run_observation_loop(
    config: RunnerConfig | None = None,
    *,
    coordinator: Coordinator = prepare_observation,
    production_executor: ProductionExecutor | None = None,
    research_executor: ResearchExecutor | None = None,
    clock: Clock | None = None,
    sleeper: Sleeper | None = None,
) -> dict[str, Any]:
    cfg = config or RunnerConfig()
    now = clock or (lambda: int(time.time()))
    sleep = sleeper or time.sleep
    production = production_executor or (lambda preparation, observation_number: execute_production_observation(preparation, observation_number, cfg.state_path))
    research = research_executor or (lambda payload: execute_research_observation(payload, cfg.research_tracker_path))

    lock = _acquire_runner_lock(cfg, now)
    if not lock["acquired"]:
        return lock

    summary: dict[str, Any] = {"status": "STARTED", "cycles": [], "runner_state_path": str(cfg.runner_state_path)}
    try:
        recovered = _resume_pending_research(cfg, research, now)
        if recovered:
            summary["recovered_pending_research"] = recovered
        cycles = 0
        while cfg.max_cycles is None or cycles < cfg.max_cycles:
            cycle = run_observation_cycle(
                cfg,
                coordinator=coordinator,
                production_executor=production,
                research_executor=research,
                clock=now,
            )
            summary["cycles"].append(cycle)
            cycles += 1
            if cfg.max_cycles is not None and cycles >= cfg.max_cycles:
                break
            next_run = int(cycle.get("next_scheduled_run") or _next_interval_time(now(), cfg))
            delay = max(0, next_run - now())
            if delay > 0:
                _update_runner_state(cfg, now, status="IDLE", next_scheduled_run=next_run)
                sleep(delay)
        summary["status"] = "STOPPED"
        return summary
    except KeyboardInterrupt:
        summary["status"] = "STOPPED"
        summary["interrupted"] = True
        return summary
    finally:
        _update_runner_state(cfg, now, status="STOPPED", stopped_at=now())


def run_observation_cycle(
    config: RunnerConfig,
    *,
    coordinator: Coordinator = prepare_observation,
    production_executor: ProductionExecutor,
    research_executor: ResearchExecutor,
    clock: Clock | None = None,
) -> dict[str, Any]:
    now = clock or (lambda: int(time.time()))
    cycle_id = _cycle_id(now())
    started_at = now()
    before_production = _sha256_or_none(config.state_path)
    before_research = _sha256_or_none(config.research_tracker_path)
    observation_number = next_observation_number(config.research_tracker_path)

    _update_runner_state(
        config,
        now,
        status="PREPARING",
        last_cycle_started_at=started_at,
        current_cycle_id=cycle_id,
        total_cycles_delta=1,
    )
    preparation = coordinator(
        output_dir=config.output_dir,
        state_path=config.state_path,
        research_tracker_path=config.research_tracker_path,
        liquidation_root=config.liquidation_root,
    )
    status = preparation.get("status")
    cycle: dict[str, Any] = {
        "cycle_id": cycle_id,
        "started_at": started_at,
        "preparation_status": status,
        "canonical_checkpoint": preparation.get("canonical_checkpoint"),
        "observation_number": None,
        "production_result": "NOT_ATTEMPTED",
        "research_result": "NOT_ATTEMPTED",
        "blocker": preparation.get("reason"),
    }
    if status == READY:
        if config.dry_run:
            cycle.update(
                {
                    "observation_number": observation_number,
                    "production_result": "DRY_RUN_NOT_PERSISTED",
                    "research_result": "DRY_RUN_NOT_PERSISTED",
                    "blocker": None,
                    "next_scheduled_run": _next_interval_time(now, config),
                }
            )
            _update_runner_state(config, now, status="IDLE", last_cycle_finished_at=now(), dry_run=True)
        else:
            cycle.update(_execute_ready_cycle(config, preparation, observation_number, production_executor, research_executor, now))
    elif status == WAITING_FOR_M5_CLOSE:
        next_run = int(preparation.get("next_required_checkpoint") or now()) + WAITING_SAFETY_DELAY_SECONDS
        cycle.update({"next_scheduled_run": next_run, "blocker": WAITING_FOR_M5_CLOSE})
        _update_runner_state(config, now, status=WAITING_FOR_M5_CLOSE, waiting_cycles_delta=1, next_scheduled_run=next_run, last_cycle_finished_at=now())
    else:
        next_run = _next_interval_time(now, config)
        cycle.update({"next_scheduled_run": next_run, "blocker": preparation.get("reason") or BLOCKED})
        _update_runner_state(config, now, status="BLOCKED", blocked_cycles_delta=1, last_blocker=cycle["blocker"], next_scheduled_run=next_run, last_cycle_finished_at=now())

    cycle["finished_at"] = now()
    cycle["production_hash_unchanged_if_no_ready"] = before_production == _sha256_or_none(config.state_path) if status != READY or config.dry_run else None
    cycle["research_hash_unchanged_if_no_ready"] = before_research == _sha256_or_none(config.research_tracker_path) if status != READY or config.dry_run else None
    _append_runner_log(config.runner_log_path, cycle)
    return cycle


def observation_runner_status(path: Path = Path("artifact/observation-runner.json"), *, clock: Clock | None = None) -> dict[str, Any]:
    now = clock or (lambda: int(time.time()))
    state = _read_json_or_empty(path)
    pid = state.get("pid")
    running = bool(pid and state.get("status") in RUNNER_ACTIVE_STATUSES and _pid_is_live(int(pid)))
    return {
        "schema": RUNNER_SCHEMA,
        "running": running,
        "pid": pid,
        "status": state.get("status", "STOPPED"),
        "started_at": state.get("started_at"),
        "stopped_at": state.get("stopped_at"),
        "last_heartbeat_at": state.get("last_heartbeat_at"),
        "last_cycle": {
            "started_at": state.get("last_cycle_started_at"),
            "finished_at": state.get("last_cycle_finished_at"),
        },
        "last_successful_observation": state.get("last_successful_observation"),
        "next_scheduled_run": state.get("next_scheduled_run"),
        "last_blocker": state.get("last_blocker"),
        "total_cycles": int(state.get("total_cycles") or 0),
        "successful_observations": int(state.get("successful_observations") or 0),
        "waiting_cycles": int(state.get("waiting_cycles") or 0),
        "blocked_cycles": int(state.get("blocked_cycles") or 0),
        "stale": _is_stale_state(state, now()),
    }


def execute_production_observation(preparation: dict[str, Any], observation_number: int, state_path: Path) -> dict[str, Any]:
    market_path = Path(str(preparation["market_path"]))
    external_path = Path(str(preparation["external_path"]))
    reviews = review_snapshot(market_path, state_path, enforce_replay_coverage=True)
    if "REPLAY_DATA_GAP" in reviews:
        raise ValueError("REPLAY_DATA_GAP")
    frames = _load_frames(market_path)
    opportunity_snapshots = {symbol: extract_opportunity_snapshot(review, frames[symbol]) for symbol, review in reviews.items()}
    payload = {
        "observation_number": observation_number,
        "market_path": str(market_path),
        "external_path": str(external_path),
        "reviews": reviews,
        "opportunity_snapshots": opportunity_snapshots,
        "external_evidence": _external_by_symbol(external_path),
        "production_hash": _sha256_or_none(state_path),
    }
    return payload


def execute_research_observation(payload: dict[str, Any], research_tracker_path: Path) -> dict[str, Any]:
    frames = _load_frames(Path(str(payload["market_path"])))
    return apply_missed_opportunity_observation(
        reviews=payload["reviews"],
        frames=frames,
        opportunity_snapshots=payload["opportunity_snapshots"],
        external_evidence=payload.get("external_evidence"),
        observation_number=int(payload["observation_number"]),
        store_path=research_tracker_path,
        persist=True,
    )


def next_observation_number(research_tracker_path: Path) -> int:
    latest = 0
    if research_tracker_path.exists():
        data = json.loads(research_tracker_path.read_text(encoding="utf-8"))
        for record in data.get("records", []):
            for snapshot in record.get("snapshots", []):
                try:
                    latest = max(latest, int(snapshot.get("observation_number") or 0))
                except (TypeError, ValueError):
                    continue
    return latest + 1 if latest else 1


def cadence_seconds(timestamp: int, weekday_minutes: int = DEFAULT_WEEKDAY_INTERVAL_MINUTES, weekend_minutes: int = DEFAULT_WEEKEND_INTERVAL_MINUTES) -> int:
    dt = datetime.fromtimestamp(timestamp, timezone.utc)
    minutes = weekend_minutes if dt.weekday() >= 5 else weekday_minutes
    return int(minutes) * 60


def _execute_ready_cycle(
    config: RunnerConfig,
    preparation: dict[str, Any],
    observation_number: int,
    production_executor: ProductionExecutor,
    research_executor: ResearchExecutor,
    clock: Clock,
) -> dict[str, Any]:
    _update_runner_state(config, clock, status="REVIEWING")
    production_payload = production_executor(preparation, observation_number)
    pending = {
        "schema": "observation-runner-pending-research.v1",
        "observation_number": observation_number,
        "payload": production_payload,
    }
    _update_runner_state(config, clock, status="PERSISTING", pending_research=pending)
    research_report = research_executor(production_payload)
    if research_report.get("research_persistence") == "FAIL":
        raise ValueError(research_report.get("message") or "research persistence failed")
    _update_runner_state(
        config,
        clock,
        status="IDLE",
        pending_research=None,
        last_successful_observation=observation_number,
        successful_observations_delta=1,
        last_cycle_finished_at=clock(),
        next_scheduled_run=_next_interval_time(clock, config),
    )
    return {
        "observation_number": observation_number,
        "production_result": "PASS",
        "research_result": research_report.get("research_persistence", "PASS"),
        "blocker": None,
        "next_scheduled_run": _next_interval_time(clock, config),
    }


def _resume_pending_research(config: RunnerConfig, research_executor: ResearchExecutor, clock: Clock) -> dict[str, Any] | None:
    state = _read_json_or_empty(config.runner_state_path)
    pending = state.get("pending_research")
    if not isinstance(pending, dict):
        return None
    payload = pending.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("production_hash") and payload.get("production_hash") != _sha256_or_none(config.state_path):
        _update_runner_state(config, clock, status="BLOCKED", last_blocker="PENDING_RESEARCH_PRODUCTION_HASH_MISMATCH")
        return {"status": "BLOCKED", "reason": "PENDING_RESEARCH_PRODUCTION_HASH_MISMATCH"}
    report = research_executor(payload)
    if report.get("research_persistence") == "FAIL":
        _update_runner_state(config, clock, status="BLOCKED", last_blocker=report.get("message") or "pending research failed")
        return {"status": "BLOCKED", "reason": report.get("message") or "pending research failed"}
    _update_runner_state(config, clock, status="IDLE", pending_research=None, last_successful_observation=pending.get("observation_number"))
    return {"status": "PASS", "observation_number": pending.get("observation_number")}


def _acquire_runner_lock(config: RunnerConfig, clock: Clock) -> dict[str, Any]:
    state = _read_json_or_empty(config.runner_state_path)
    pid = state.get("pid")
    active = bool(pid and state.get("status") in RUNNER_ACTIVE_STATUSES and _pid_is_live(int(pid)) and not _is_stale_state(state, clock(), config.stale_lock_seconds))
    if active and int(pid) != os.getpid():
        return {"acquired": False, "status": RUNNER_ALREADY_ACTIVE, "pid": pid, "runner_state_path": str(config.runner_state_path)}
    recovered_stale = bool(pid and state.get("status") in RUNNER_ACTIVE_STATUSES and not active)
    _update_runner_state(config, clock, status="STARTING", pid=os.getpid(), started_at=clock(), stopped_at=None, stale_lock_recovered=recovered_stale)
    return {"acquired": True, "status": "STARTING", "stale_lock_recovered": recovered_stale}


def _update_runner_state(config: RunnerConfig, clock: Clock, **updates: Any) -> None:
    path = config.runner_state_path
    state = _read_json_or_empty(path)
    state.setdefault("schema", RUNNER_SCHEMA)
    state.setdefault("pid", os.getpid())
    state["last_heartbeat_at"] = clock()
    for key, value in updates.items():
        if key.endswith("_delta"):
            target = key.removesuffix("_delta")
            state[target] = int(state.get(target) or 0) + int(value)
        elif value is None and key == "pending_research":
            state.pop(key, None)
        else:
            state[key] = value
    atomic_write_json(path, state)


def _append_runner_log(path: Path, entry: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, sort_keys=True) + "\n")


def _next_interval_time(clock: Clock, config: RunnerConfig) -> int:
    current = clock()
    return current + cadence_seconds(current, config.weekday_interval_minutes, config.weekend_interval_minutes)


def _cycle_id(timestamp: int) -> str:
    return f"cycle-{timestamp}-{os.getpid()}"


def _load_frames(path: Path) -> dict[str, dict[str, MarketDataFrame]]:
    snapshot = load_snapshot(path)
    return {
        symbol: {tf: to_market_data_frame(raw_frames[tf]) for tf in TIMEFRAMES}
        for symbol, raw_frames in snapshot.items()
        if symbol in ACTIVE_SYMBOLS
    }


def _external_by_symbol(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    symbols = payload.get("symbols") or {}
    return {
        symbol: dict((value or {}).get("evidence") or {})
        for symbol, value in symbols.items()
        if isinstance(value, dict)
    }


def _read_json_or_empty(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _sha256_or_none(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _pid_is_live(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {int(pid)}", "/FO", "CSV", "/NH"],
            capture_output=True,
            text=True,
            check=False,
        )
        return str(int(pid)) in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _is_stale_state(state: dict[str, Any], now: int, stale_after: int = 6 * 60 * 60) -> bool:
    heartbeat = state.get("last_heartbeat_at")
    if heartbeat is None:
        return False
    try:
        return now - int(heartbeat) > stale_after
    except (TypeError, ValueError):
        return False
