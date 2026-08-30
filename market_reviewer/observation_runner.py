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
COMMIT_JOURNAL_SCHEMA = "observation-commit-journal.v1"
PRODUCTION_HEAD_SCHEMA = "production-observation-head.v1"
RUNNER_ACTIVE_STATUSES = {"STARTING", "IDLE", "PREPARING", "WAITING_FOR_M5_CLOSE", "REVIEWING", "PERSISTING", "PENDING_RESEARCH_RECOVERY", "BLOCKED"}
RUNNER_ALREADY_ACTIVE = "RUNNER_ALREADY_ACTIVE"
PENDING_RESEARCH_RECOVERY = "PENDING_RESEARCH_RECOVERY"
PENDING_RESEARCH_RECOVERY_NO_JOURNAL = "PENDING_RESEARCH_RECOVERY_NO_JOURNAL"
BLOCKED_INVALID_OBSERVATION_STATE = "BLOCKED_INVALID_OBSERVATION_STATE"
BLOCKED_RECOVERY_GAP = "BLOCKED_RECOVERY_GAP"
BLOCKED_RECOVERY_METADATA_MISSING = "BLOCKED_RECOVERY_METADATA_MISSING"
BLOCKED_RECOVERY_EVIDENCE_MISSING = "BLOCKED_RECOVERY_EVIDENCE_MISSING"
BLOCKED_RESEARCH_IDENTITY_MISMATCH = "BLOCKED_RESEARCH_IDENTITY_MISMATCH"
BLOCKED_STALE_PREPARED_BASELINE = "BLOCKED_STALE_PREPARED_BASELINE"
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

    @property
    def commit_journal_path(self) -> Path:
        return self.output_dir / "observation-commit-journal.json"

    @property
    def production_head_path(self) -> Path:
        return self.output_dir / "production-observation-head.json"


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
        recovered = _reconcile_observation_transactions(cfg, research, now)
        if recovered:
            summary["recovered_pending_research"] = recovered
            if str(recovered.get("status", "")).startswith("BLOCKED"):
                summary["status"] = recovered["status"]
                return summary
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
    reconciled = _reconcile_observation_transactions(config, research_executor, now)
    if reconciled and str(reconciled.get("status", "")).startswith("BLOCKED"):
        cycle = {
            "cycle_id": cycle_id,
            "started_at": started_at,
            "finished_at": now(),
            "preparation_status": "BLOCKED",
            "canonical_checkpoint": None,
            "observation_number": None,
            "production_result": "NOT_ATTEMPTED",
            "research_result": "NOT_ATTEMPTED",
            "blocker": reconciled["status"],
            "next_scheduled_run": _next_interval_time(now, config),
        }
        _update_runner_state(config, now, status="BLOCKED", blocked_cycles_delta=1, last_blocker=reconciled["status"], last_cycle_finished_at=cycle["finished_at"])
        _append_runner_log(config.runner_log_path, cycle)
        return cycle
    observation_number = next_observation_number(config.research_tracker_path, config.commit_journal_path, config.production_head_path)

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
            cycle.update(_execute_ready_cycle(config, preparation, observation_number, production_executor, research_executor, now, cycle_id))
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


def observation_runner_status(
    path: Path = Path("artifact/observation-runner.json"),
    *,
    state_path: Path = Path("reviews/thesis-baseline.json"),
    research_tracker_path: Path = DEFAULT_TRACKER_PATH,
    journal_path: Path | None = None,
    production_head_path: Path | None = None,
    clock: Clock | None = None,
) -> dict[str, Any]:
    now = clock or (lambda: int(time.time()))
    state = _read_json_or_empty(path)
    resolved_journal_path = journal_path or path.parent / "observation-commit-journal.json"
    resolved_head_path = production_head_path or path.parent / "production-observation-head.json"
    journal = _read_journal(resolved_journal_path)
    head = _read_production_head(resolved_head_path)
    tx = _active_transaction(journal)
    latest_production = latest_production_observation(journal, research_tracker_path, production_head=head)
    latest_research = latest_research_observation(research_tracker_path)
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
        "pending_research_recovery": bool(tx and tx.get("status") in {"PRODUCTION_COMMITTED", "RESEARCH_PENDING"}),
        "pending_observation_number": tx.get("observation_number") if tx else None,
        "pending_cycle_id": tx.get("cycle_id") if tx else None,
        "production_latest_observation": latest_production,
        "research_latest_observation": latest_research,
        "production_head_observation": head.get("observation_number"),
        "production_head_checkpoint": head.get("canonical_checkpoint"),
        "production_head_hash": head.get("production_state_sha256"),
        "recovery_status": _recovery_status(latest_production, latest_research, tx, head),
        "recovery_last_error": state.get("recovery_last_error") or (tx or {}).get("recovery_last_error"),
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


def next_observation_number(research_tracker_path: Path, journal_path: Path | None = None, production_head_path: Path | None = None) -> int:
    if production_head_path is not None:
        latest = latest_production_observation(_read_journal(journal_path) if journal_path is not None else {"transactions": []}, research_tracker_path, production_head=_read_production_head(production_head_path))
        return latest + 1 if latest else 1
    if journal_path is not None:
        latest = latest_production_observation(_read_journal(journal_path), research_tracker_path)
        return latest + 1 if latest else 1
    latest = latest_research_observation(research_tracker_path)
    return latest + 1 if latest else 1


def latest_research_observation(research_tracker_path: Path) -> int:
    latest = 0
    if research_tracker_path.exists():
        data = json.loads(research_tracker_path.read_text(encoding="utf-8"))
        for record in data.get("records", []):
            for snapshot in record.get("snapshots", []):
                try:
                    latest = max(latest, int(snapshot.get("observation_number") or 0))
                except (TypeError, ValueError):
                    continue
    return latest


def latest_production_observation(journal: dict[str, Any], research_tracker_path: Path, production_head: dict[str, Any] | None = None) -> int:
    latest = 0
    if production_head and production_head.get("schema") == PRODUCTION_HEAD_SCHEMA:
        try:
            latest = max(latest, int(production_head.get("observation_number") or 0))
        except (TypeError, ValueError):
            pass
    for tx in journal.get("transactions", []):
        if not isinstance(tx, dict) or tx.get("status") not in {"PRODUCTION_COMMITTED", "RESEARCH_PENDING", "COMPLETE"}:
            continue
        try:
            latest = max(latest, int(tx.get("observation_number") or 0))
        except (TypeError, ValueError):
            continue
    return latest or latest_research_observation(research_tracker_path)


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
    cycle_id: str,
) -> dict[str, Any]:
    starting_production_hash = _sha256_or_none(config.state_path)
    starting_research_hash = _sha256_or_none(config.research_tracker_path)
    _write_observation_intent(
        config,
        {
            "cycle_id": cycle_id,
            "observation_number": observation_number,
            "canonical_checkpoint": preparation.get("canonical_checkpoint"),
            "market_path": preparation.get("market_path"),
            "external_path": preparation.get("external_path"),
            "starting_production_hash": starting_production_hash,
            "starting_research_hash": starting_research_hash,
            "production_previous_review_timestamp": _previous_review_timestamps(config.state_path),
            "status": "PREPARED",
            "research_status": "PENDING",
            "prepared_at": clock(),
        },
    )
    _update_runner_state(config, clock, status="REVIEWING")
    _update_observation_intent(config, observation_number, status="PRODUCTION_COMMITTING", production_committing_at=clock())
    production_payload = production_executor(preparation, observation_number)
    production_hash = production_payload.get("production_hash") or _sha256_or_none(config.state_path)
    _write_production_head(
        config.production_head_path,
        {
            "schema": PRODUCTION_HEAD_SCHEMA,
            "observation_number": observation_number,
            "canonical_checkpoint": preparation.get("canonical_checkpoint"),
            "production_previous_review_timestamp": _previous_review_timestamps(config.state_path),
            "production_state_sha256": production_hash,
            "cycle_id": cycle_id,
            "committed_at": clock(),
        },
    )
    pending = {
        "schema": "observation-runner-pending-research.v1",
        "observation_number": observation_number,
        "payload": production_payload,
    }
    _update_observation_intent(
        config,
        observation_number,
        status="RESEARCH_PENDING",
        research_status="PENDING",
        production_hash=production_hash,
        production_committed_at=clock(),
        recovery_payload=production_payload,
    )
    _update_runner_state(config, clock, status="PERSISTING", pending_research=pending)
    research_report = research_executor(production_payload)
    if research_report.get("research_persistence") == "FAIL":
        _update_observation_intent(config, observation_number, recovery_last_error=research_report.get("message") or "research persistence failed")
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
    _update_observation_intent(
        config,
        observation_number,
        status="COMPLETE",
        research_status="COMPLETE",
        research_completed_at=clock(),
        research_state_sha256=_sha256_or_none(config.research_tracker_path),
        recovery_payload=production_payload,
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


def _reconcile_observation_transactions(config: RunnerConfig, research_executor: ResearchExecutor, clock: Clock) -> dict[str, Any] | None:
    legacy = _resume_pending_research(config, research_executor, clock)
    if legacy:
        return legacy
    journal = _read_journal(config.commit_journal_path)
    tx = _active_transaction(journal)
    head = _bootstrap_production_head_if_aligned(config, clock)
    latest_research = latest_research_observation(config.research_tracker_path)
    latest_production = latest_production_observation(journal, config.research_tracker_path, production_head=head)
    production_hash = _sha256_or_none(config.state_path)
    head_observation = _optional_int(head.get("observation_number")) if head else None
    head_hash = head.get("production_state_sha256") if head else None
    journal_latest = _latest_journal_production_observation(journal)

    if head and head_hash and production_hash and head_hash != production_hash:
        _update_runner_state(config, clock, status="BLOCKED", last_blocker=BLOCKED_RECOVERY_GAP, recovery_last_error="production head hash mismatch")
        return {"status": BLOCKED_RECOVERY_GAP, "reason": "PRODUCTION_HEAD_HASH_MISMATCH", "production_latest_observation": head_observation, "research_latest_observation": latest_research}
    if head_observation and journal_latest and journal_latest != head_observation and (tx or journal_latest > head_observation):
        _update_runner_state(config, clock, status="BLOCKED", last_blocker=BLOCKED_RECOVERY_GAP, recovery_last_error="production head and journal disagree")
        return {"status": BLOCKED_RECOVERY_GAP, "reason": "HEAD_JOURNAL_MISMATCH", "production_latest_observation": head_observation, "journal_latest_observation": journal_latest, "research_latest_observation": latest_research}
    if latest_research > latest_production:
        _update_runner_state(config, clock, status="BLOCKED", last_blocker=BLOCKED_INVALID_OBSERVATION_STATE, recovery_last_error="research ahead of production")
        return {"status": BLOCKED_INVALID_OBSERVATION_STATE, "production_latest_observation": latest_production, "research_latest_observation": latest_research}
    if latest_production - latest_research > 1:
        _update_runner_state(config, clock, status="BLOCKED", last_blocker=BLOCKED_RECOVERY_GAP, recovery_last_error="production ahead by more than one observation")
        return {"status": BLOCKED_RECOVERY_GAP, "production_latest_observation": latest_production, "research_latest_observation": latest_research}
    if not tx:
        if head_observation and head_observation > latest_research:
            _update_runner_state(config, clock, status="BLOCKED", last_blocker=BLOCKED_RECOVERY_METADATA_MISSING, recovery_last_error="production ahead without recovery journal")
            return {"status": BLOCKED_RECOVERY_METADATA_MISSING, "recovery": PENDING_RESEARCH_RECOVERY_NO_JOURNAL, "production_latest_observation": head_observation, "research_latest_observation": latest_research}
        return None

    observation_number = int(tx.get("observation_number") or 0)
    if head_observation and tx.get("status") in {"PRODUCTION_COMMITTED", "RESEARCH_PENDING", "COMPLETE"} and observation_number != head_observation:
        _update_runner_state(config, clock, status="BLOCKED", last_blocker=BLOCKED_RECOVERY_GAP, recovery_last_error="active journal observation does not match production head")
        return {"status": BLOCKED_RECOVERY_GAP, "observation_number": observation_number, "reason": "ACTIVE_JOURNAL_HEAD_MISMATCH"}

    if tx.get("status") in {"PREPARED", "PRODUCTION_COMMITTING"}:
        current_research_hash = _sha256_or_none(config.research_tracker_path)
        production_unchanged = production_hash == tx.get("starting_production_hash")
        research_unchanged = current_research_hash == tx.get("starting_research_hash")
        if production_unchanged and research_unchanged:
            _update_observation_intent(config, observation_number, status="ABANDONED", abandoned_at=clock(), abandon_reason="PRODUCTION_NOT_COMMITTED")
            return {"status": "PREPARED_NOT_COMMITTED", "observation_number": observation_number}
        if production_unchanged or current_research_hash != tx.get("starting_research_hash"):
            _update_runner_state(config, clock, status="BLOCKED", last_blocker=BLOCKED_STALE_PREPARED_BASELINE, recovery_last_error="prepared baseline drift")
            _update_observation_intent(config, observation_number, recovery_last_error=BLOCKED_STALE_PREPARED_BASELINE)
            return {"status": BLOCKED_STALE_PREPARED_BASELINE, "observation_number": observation_number}
        if not head_observation or head_observation != observation_number:
            _update_runner_state(config, clock, status="BLOCKED", last_blocker=BLOCKED_RECOVERY_METADATA_MISSING, recovery_last_error="production changed but production head is missing")
            return {"status": BLOCKED_RECOVERY_METADATA_MISSING, "observation_number": observation_number, "reason": "PRODUCTION_HEAD_MISSING_AFTER_HASH_CHANGE"}
        _update_observation_intent(
            config,
            observation_number,
            status="RESEARCH_PENDING",
            research_status="PENDING",
            production_hash=production_hash,
            production_committed_at=clock(),
            recovery_inferred_from_hash_change=True,
        )
        tx = _active_transaction(_read_journal(config.commit_journal_path)) or tx
    if tx.get("status") in {"PRODUCTION_COMMITTED", "RESEARCH_PENDING"} and tx.get("production_hash") and tx.get("production_hash") != production_hash:
        _update_runner_state(config, clock, status="BLOCKED", last_blocker=BLOCKED_RECOVERY_GAP, recovery_last_error="production hash mismatch for pending observation")
        return {"status": BLOCKED_RECOVERY_GAP, "observation_number": observation_number, "reason": "PRODUCTION_HASH_MISMATCH"}
    identity_status = _research_identity_status(config.research_tracker_path, tx)
    if observation_number and identity_status == "MATCH":
        _update_observation_intent(
            config,
            observation_number,
            status="COMPLETE",
            research_status="COMPLETE",
            research_completed_at=clock(),
            research_state_sha256=_sha256_or_none(config.research_tracker_path),
        )
        _update_runner_state(config, clock, status="IDLE", pending_research=None, last_successful_observation=observation_number)
        return {"status": "PASS", "observation_number": observation_number, "recovery": "RESEARCH_ALREADY_PRESENT"}
    if identity_status == "MISMATCH":
        _update_runner_state(config, clock, status="BLOCKED", last_blocker=BLOCKED_RESEARCH_IDENTITY_MISMATCH, recovery_last_error="research observation identity mismatch")
        return {"status": BLOCKED_RESEARCH_IDENTITY_MISMATCH, "observation_number": observation_number}
    if tx.get("status") in {"PRODUCTION_COMMITTED", "RESEARCH_PENDING"}:
        _update_runner_state(
            config,
            clock,
            status=PENDING_RESEARCH_RECOVERY,
            pending_research_recovery=True,
            pending_observation_number=observation_number,
            pending_cycle_id=tx.get("cycle_id"),
        )
        if not _transaction_has_recovery_evidence(config, tx):
            _update_runner_state(config, clock, status="BLOCKED", last_blocker=BLOCKED_RECOVERY_EVIDENCE_MISSING, recovery_last_error="frozen recovery evidence missing")
            return {"status": BLOCKED_RECOVERY_EVIDENCE_MISSING, "observation_number": observation_number}
        payload = _recovery_payload_from_transaction(config, tx)
        report = research_executor(payload)
        if report.get("research_persistence") == "FAIL":
            _update_observation_intent(config, observation_number, recovery_last_error=report.get("message") or "research persistence failed")
            _update_runner_state(config, clock, status="BLOCKED", last_blocker="BLOCKED_RECOVERY", recovery_last_error=report.get("message") or "research persistence failed")
            return {"status": "BLOCKED_RECOVERY", "observation_number": observation_number, "reason": report.get("message") or "research persistence failed"}
        _update_observation_intent(
            config,
            observation_number,
            status="COMPLETE",
            research_status="COMPLETE",
            research_completed_at=clock(),
            research_state_sha256=_sha256_or_none(config.research_tracker_path),
            recovery_payload=payload,
        )
        _update_runner_state(
            config,
            clock,
            status="IDLE",
            pending_research=None,
            pending_research_recovery=False,
            last_successful_observation=observation_number,
            successful_observations_delta=1,
        )
        return {"status": "PASS", "observation_number": observation_number, "recovery": PENDING_RESEARCH_RECOVERY}
    return None


def _latest_journal_production_observation(journal: dict[str, Any]) -> int:
    latest = 0
    for tx in journal.get("transactions", []):
        if not isinstance(tx, dict) or tx.get("status") not in {"PRODUCTION_COMMITTED", "RESEARCH_PENDING", "COMPLETE"}:
            continue
        value = _optional_int(tx.get("observation_number"))
        if value:
            latest = max(latest, value)
    return latest


def _transaction_has_recovery_evidence(config: RunnerConfig, transaction: dict[str, Any]) -> bool:
    if isinstance(transaction.get("recovery_payload"), dict):
        return True
    market_path = Path(str(transaction.get("market_path") or ""))
    external_path = Path(str(transaction.get("external_path") or ""))
    return market_path.exists() and external_path.exists()


def _research_identity_status(path: Path, transaction: dict[str, Any]) -> str:
    observation_number = _optional_int(transaction.get("observation_number"))
    if not observation_number or not path.exists():
        return "MISSING"
    data = json.loads(path.read_text(encoding="utf-8"))
    matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for record in data.get("records", []):
        if not isinstance(record, dict):
            continue
        for snapshot in record.get("snapshots", []):
            if not isinstance(snapshot, dict):
                continue
            if _optional_int(snapshot.get("observation_number")) == observation_number:
                matches.append((record, snapshot))
    if not matches:
        return "MISSING"
    expected = _expected_research_identity(transaction)
    checkpoint = expected.get("canonical_checkpoint")
    expected_symbols = expected.get("symbols") or {}
    for record, snapshot in matches:
        symbol = str(record.get("symbol") or snapshot.get("symbol") or "")
        snapshot_timestamp = _optional_int(snapshot.get("snapshot_timestamp"))
        if checkpoint and snapshot_timestamp != checkpoint:
            return "MISMATCH"
        symbol_expected = expected_symbols.get(symbol) if isinstance(expected_symbols, dict) else None
        if isinstance(symbol_expected, dict):
            tracker_id = symbol_expected.get("tracker_id")
            if tracker_id and record.get("tracker_id") != tracker_id:
                return "MISMATCH"
            sequence_id = symbol_expected.get("sequence_id")
            if sequence_id and snapshot.get("production_sequence_id") and snapshot.get("production_sequence_id") != sequence_id:
                return "MISMATCH"
            opportunity_id = symbol_expected.get("opportunity_id")
            if opportunity_id and snapshot.get("opportunity_id") and snapshot.get("opportunity_id") != opportunity_id:
                return "MISMATCH"
    if expected_symbols:
        present_symbols = {str(record.get("symbol") or snapshot.get("symbol") or "") for record, snapshot in matches}
        missing_symbols = set(expected_symbols) - present_symbols
        if missing_symbols:
            return "MISMATCH"
    return "MATCH"


def _expected_research_identity(transaction: dict[str, Any]) -> dict[str, Any]:
    identity = transaction.get("research_identity")
    if isinstance(identity, dict):
        return identity
    expected: dict[str, Any] = {"canonical_checkpoint": _optional_int(transaction.get("canonical_checkpoint")), "symbols": {}}
    payload = transaction.get("recovery_payload")
    if isinstance(payload, dict):
        opportunities = payload.get("opportunity_snapshots") or {}
        if isinstance(opportunities, dict):
            for symbol, snapshot in opportunities.items():
                if not isinstance(snapshot, dict):
                    continue
                expected["symbols"][str(symbol)] = {
                    "opportunity_id": snapshot.get("opportunity_id"),
                    "sequence_id": snapshot.get("sequence_id"),
                }
    return expected


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _read_journal(path: Path) -> dict[str, Any]:
    data = _read_json_or_empty(path)
    if data.get("schema") != COMMIT_JOURNAL_SCHEMA or not isinstance(data.get("transactions"), list):
        return {"schema": COMMIT_JOURNAL_SCHEMA, "transactions": []}
    return data


def _write_journal(path: Path, journal: dict[str, Any]) -> None:
    journal.setdefault("schema", COMMIT_JOURNAL_SCHEMA)
    journal.setdefault("transactions", [])
    atomic_write_json(path, journal)


def _read_production_head(path: Path) -> dict[str, Any]:
    data = _read_json_or_empty(path)
    if data.get("schema") != PRODUCTION_HEAD_SCHEMA:
        return {}
    return data


def _write_production_head(path: Path, head: dict[str, Any]) -> None:
    head = dict(head)
    head.setdefault("schema", PRODUCTION_HEAD_SCHEMA)
    atomic_write_json(path, head)


def _bootstrap_production_head_if_aligned(config: RunnerConfig, clock: Clock) -> dict[str, Any]:
    head = _read_production_head(config.production_head_path)
    if head:
        return head
    latest_research = latest_research_observation(config.research_tracker_path)
    production_hash = _sha256_or_none(config.state_path)
    if latest_research != 53 or production_hash is None:
        return {}
    states, _ = load_review_state(config.state_path)
    timestamps = _previous_review_timestamps(config.state_path)
    if not states or not all(int(value or 0) > 0 for value in timestamps.values()):
        return {}
    head = {
        "schema": PRODUCTION_HEAD_SCHEMA,
        "observation_number": latest_research,
        "canonical_checkpoint": max(timestamps.values()) if timestamps else None,
        "production_previous_review_timestamp": timestamps,
        "production_state_sha256": production_hash,
        "cycle_id": "BOOTSTRAP_FROM_ALIGNED_RESEARCH",
        "committed_at": clock(),
        "bootstrap_status": "BOOTSTRAPPED_FROM_ALIGNED_RESEARCH",
    }
    _write_production_head(config.production_head_path, head)
    return head


def _write_observation_intent(config: RunnerConfig, transaction: dict[str, Any]) -> None:
    journal = _read_journal(config.commit_journal_path)
    journal.setdefault("transactions", []).append(dict(transaction))
    _write_journal(config.commit_journal_path, journal)


def _update_observation_intent(config: RunnerConfig, observation_number: int, **updates: Any) -> None:
    journal = _read_journal(config.commit_journal_path)
    transactions = journal.setdefault("transactions", [])
    for tx in reversed(transactions):
        if isinstance(tx, dict) and int(tx.get("observation_number") or 0) == int(observation_number):
            tx.update(updates)
            _write_journal(config.commit_journal_path, journal)
            return
    replacement = {"observation_number": observation_number, **updates}
    transactions.append(replacement)
    _write_journal(config.commit_journal_path, journal)


def _active_transaction(journal: dict[str, Any]) -> dict[str, Any] | None:
    active_statuses = {"PREPARED", "PRODUCTION_COMMITTING", "PRODUCTION_COMMITTED", "RESEARCH_PENDING"}
    for tx in reversed(journal.get("transactions", [])):
        if isinstance(tx, dict) and tx.get("status") in active_statuses:
            return tx
    return None


def _recovery_status(latest_production: int, latest_research: int, tx: dict[str, Any] | None, production_head: dict[str, Any] | None = None) -> str:
    if latest_research > latest_production:
        return BLOCKED_INVALID_OBSERVATION_STATE
    if latest_production - latest_research > 1:
        return BLOCKED_RECOVERY_GAP
    if latest_production > latest_research and not tx and production_head:
        return BLOCKED_RECOVERY_METADATA_MISSING
    if tx and tx.get("status") in {"PRODUCTION_COMMITTED", "RESEARCH_PENDING"}:
        return PENDING_RESEARCH_RECOVERY
    return "NORMAL"


def _research_has_observation(path: Path, observation_number: int) -> bool:
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    for record in data.get("records", []):
        for snapshot in record.get("snapshots", []):
            try:
                if int(snapshot.get("observation_number") or 0) == int(observation_number):
                    return True
            except (TypeError, ValueError):
                continue
    return False


def _previous_review_timestamps(path: Path) -> dict[str, int]:
    states, _ = load_review_state(path)
    return {
        symbol: int((state or {}).get("previous_review_timestamp") or 0)
        for symbol, state in states.items()
    }


def _recovery_payload_from_transaction(config: RunnerConfig, transaction: dict[str, Any]) -> dict[str, Any]:
    payload = transaction.get("recovery_payload")
    if isinstance(payload, dict):
        return payload
    market_path = Path(str(transaction.get("market_path") or config.output_dir / "market-data-v1.json"))
    external_path = Path(str(transaction.get("external_path") or config.output_dir / "external-market-evidence-v1.json"))
    frames = _load_frames(market_path)
    states, _ = load_review_state(config.state_path)
    reviews = {
        symbol: _review_from_persisted_state(symbol, state)
        for symbol, state in states.items()
        if symbol in frames
    }
    opportunity_snapshots = {
        symbol: extract_opportunity_snapshot(review, frames[symbol])
        for symbol, review in reviews.items()
    }
    return {
        "observation_number": int(transaction.get("observation_number") or 0),
        "market_path": str(market_path),
        "external_path": str(external_path),
        "reviews": reviews,
        "opportunity_snapshots": opportunity_snapshots,
        "external_evidence": _external_by_symbol(external_path),
        "production_hash": _sha256_or_none(config.state_path),
        "recovered_from_journal": True,
    }


def _review_from_persisted_state(symbol: str, state: dict[str, Any]) -> dict[str, Any]:
    active = state.get("active_tactical_draw") if isinstance(state.get("active_tactical_draw"), dict) else None
    setup = state.get("active_setup_poi") if isinstance(state.get("active_setup_poi"), dict) else None
    return {
        "Symbol": symbol,
        "Swing_Bias": state.get("previous_bias"),
        "Current_Phase": state.get("current_phase"),
        "Market_Regime": state.get("previous_regime"),
        "State": state.get("previous_state"),
        "Sequence_ID": state.get("sequence_id"),
        "Sequence_State": state.get("sequence_state"),
        "Sequence_Started_At": str(state.get("sequence_started_at") or "NONE"),
        "Review_Timestamp": str(state.get("previous_review_timestamp") or 0),
        "Active_Tactical_Draw": _level_to_text(active),
        "Active_Draw_Selected_At": str((active or {}).get("selected_at") or "NONE"),
        "Setup_FVG": _setup_to_text(setup),
        "Eligible_Retest_Confirmed": "YES" if state.get("eligible_retest_confirmed") else "NO",
        "Eligible_Retest_Setup_ID": state.get("eligible_retest_setup_id") or "NONE",
        "Eligible_Retest_Timestamp": str(state.get("eligible_retest_timestamp") or "NONE"),
        "Missing_Evidence": [],
        "Liquidity_Events": [],
        "Structure_State": {},
        "Structural_Invalidation": {},
        "Sequence_Transitions": state.get("sequence_transition_history") or [],
    }


def _level_to_text(level: dict[str, Any] | None) -> str:
    if not level:
        return "NONE"
    return f"{level.get('type')} {level.get('price')} on {level.get('timeframe')}, formed_at={level.get('formed_at')}"


def _setup_to_text(setup: dict[str, Any] | None) -> str:
    if not setup:
        return "NONE"
    lower = setup.get("lower")
    upper = setup.get("upper")
    return f"{setup.get('direction', 'BULLISH')} SETUP_FVG {setup.get('timeframe')} {lower}-{upper} @ {setup.get('formed_at')}; status={setup.get('status', 'FRESH')}"


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
