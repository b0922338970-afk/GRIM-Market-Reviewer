"""Temporal observation preparation coordinator.

The coordinator is research/orchestration only. It may create or refresh
runtime artifacts through the existing fetch commands, but it never mutates
production review state or missed-opportunity research state.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Callable

from .external import FETCH_MODE_PRODUCTION_REPLAY, run_external_fetch
from .external_evidence_providers import run_external_evidence_fetch
from .liquidation_collector import CoverageStore, LiquidationEventStore, aggregate_liquidations
from .missed_opportunity import DEFAULT_TRACKER_PATH, load_tracker_store, pending_outcome_required_start_by_symbol
from .model import ACTIVE_SYMBOLS
from .persistence import atomic_write_json, load_review_state


READY = "READY"
BLOCKED = "BLOCKED"
WAITING_FOR_M5_CLOSE = "WAITING_FOR_M5_CLOSE"
LIQUIDATION_NOT_READY = "LIQUIDATION_NOT_READY"
MARKET_CHECKPOINT_GAP = "MARKET_CHECKPOINT_GAP"

REQUIRED_EXTERNAL_FIELDS = (
    "oi",
    "oi_change_5m",
    "oi_change_15m",
    "oi_change_1h",
    "oi_change_4h",
    "funding_rate",
    "funding_change",
    "funding_percentile",
)

MarketFetcher = Callable[[Path], Path]
ExternalFetcher = Callable[[Path], Path]
Clock = Callable[[], int]


def prepare_observation(
    *,
    output_dir: Path = Path("artifact"),
    state_path: Path = Path("reviews/thesis-baseline.json"),
    research_tracker_path: Path = DEFAULT_TRACKER_PATH,
    liquidation_root: Path = Path("artifact/liquidations"),
    market_fetcher: MarketFetcher | None = None,
    external_fetcher: ExternalFetcher | None = None,
    clock: Clock | None = None,
    symbols: tuple[str, ...] = ACTIVE_SYMBOLS,
) -> dict[str, Any]:
    """Fetch and align market/external/liquidation evidence for an observation.

    Market data is fetched first, external evidence is fetched exactly once and
    then frozen. A second market fetch is allowed only when the frozen external
    evidence requires a later closed M5 checkpoint than the first market artifact
    contains.
    """

    before_production_hash = _sha256_or_none(state_path)
    before_research_hash = _sha256_or_none(research_tracker_path)
    previous_state, loaded_from = load_review_state(state_path)
    research_required_starts = _research_required_starts(research_tracker_path)

    now = clock or (lambda: int(time.time()))
    market_fetch = market_fetcher or _production_market_fetcher(state_path, research_tracker_path)
    external_fetch = external_fetcher or _external_evidence_fetcher()

    resume_path = _resume_state_path(output_dir)
    resume = _load_waiting_resume(resume_path, before_production_hash, before_research_hash)
    if resume and resume.get("status") == "BASELINE_CHANGED":
        return _baseline_changed_result(
            resume=resume,
            state_path=state_path,
            research_tracker_path=research_tracker_path,
            before_production_hash=before_production_hash,
            before_research_hash=before_research_hash,
            loaded_from=loaded_from,
            symbols=symbols,
        )

    market_fetch_count = int((resume or {}).get("market_fetch_count") or 0) + 1
    external_fetch_count = int((resume or {}).get("external_fetch_count") or 0)
    market_path = market_fetch(output_dir)
    market_snapshot = _read_json(market_path)
    if resume:
        external_path = Path(str(resume["external_path"]))
        external_freeze = copy.deepcopy(resume["external_freeze"])
    else:
        external_fetch_count = 1
        external_path = external_fetch(output_dir)
        external_snapshot = _read_json(external_path)
        external_freeze = _external_freeze(external_snapshot, symbols)
    required_external_time = external_freeze["required_external_time"]

    selected = _select_market_checkpoint(market_snapshot, required_external_time, symbols, previous_state)
    first_initial_checkpoint = selected.get("initial_checkpoint")
    if selected["status"] != READY:
        if selected["status"] == WAITING_FOR_M5_CLOSE and int(now()) < int(selected["next_required_checkpoint"]):
            _persist_waiting_resume(
                resume_path,
                status=WAITING_FOR_M5_CLOSE,
                reason=WAITING_FOR_M5_CLOSE,
                external_path=external_path,
                external_freeze=external_freeze,
                selected=selected,
                before_production_hash=before_production_hash,
                before_research_hash=before_research_hash,
                market_fetch_count=market_fetch_count,
                external_fetch_count=external_fetch_count,
            )
        else:
            market_fetch_count += 1
            market_path = market_fetch(output_dir)
            market_snapshot = _read_json(market_path)
            selected = _select_market_checkpoint(market_snapshot, required_external_time, symbols, previous_state)
            selected["initial_checkpoint"] = first_initial_checkpoint

    if selected["status"] != READY:
        return _result(
            status=WAITING_FOR_M5_CLOSE if selected["status"] == WAITING_FOR_M5_CLOSE else BLOCKED,
            reason=selected["reason"],
            market_path=market_path,
            external_path=external_path,
            market_snapshot=market_snapshot,
            external_freeze=external_freeze,
            selected=selected,
            state_path=state_path,
            research_tracker_path=research_tracker_path,
            loaded_from=loaded_from,
            previous_state=previous_state,
            research_required_starts=research_required_starts,
            before_production_hash=before_production_hash,
            before_research_hash=before_research_hash,
            market_fetch_count=market_fetch_count,
            external_fetch_count=external_fetch_count,
            liquidation_root=liquidation_root,
            symbols=symbols,
        )

    checkpoint = int(selected["canonical_checkpoint"])
    _clear_resume_state(resume_path)
    external_ready = _external_temporal_ready(external_freeze, checkpoint)
    liquidation = _liquidation_readiness(liquidation_root, checkpoint, symbols)
    final_status = READY if external_ready["ready"] and liquidation["ready"] else BLOCKED
    reason = None
    if not external_ready["ready"]:
        reason = "EXTERNAL_EVIDENCE_TEMPORAL_BLOCK"
    elif not liquidation["ready"]:
        reason = LIQUIDATION_NOT_READY

    return _result(
        status=final_status,
        reason=reason,
        market_path=market_path,
        external_path=external_path,
        market_snapshot=market_snapshot,
        external_freeze=external_freeze,
        selected=selected,
        state_path=state_path,
        research_tracker_path=research_tracker_path,
        loaded_from=loaded_from,
        previous_state=previous_state,
        research_required_starts=research_required_starts,
        before_production_hash=before_production_hash,
        before_research_hash=before_research_hash,
        market_fetch_count=market_fetch_count,
        external_fetch_count=external_fetch_count,
        liquidation_root=liquidation_root,
        symbols=symbols,
        external_ready=external_ready,
        liquidation=liquidation,
    )


def _production_market_fetcher(state_path: Path, research_tracker_path: Path) -> MarketFetcher:
    def fetch(output_dir: Path) -> Path:
        return run_external_fetch(
            output_dir,
            state_path=state_path,
            mode=FETCH_MODE_PRODUCTION_REPLAY,
            research_tracker_path=research_tracker_path,
        )

    return fetch


def _external_evidence_fetcher() -> ExternalFetcher:
    def fetch(output_dir: Path) -> Path:
        return run_external_evidence_fetch(output_dir)

    return fetch


def _resume_state_path(output_dir: Path) -> Path:
    return output_dir / "observation-preparation-resume.json"


def _load_waiting_resume(path: Path, production_hash: str | None, research_hash: str | None) -> dict[str, Any] | None:
    if not path.exists():
        return None
    resume = _read_json(path)
    if resume.get("status") != WAITING_FOR_M5_CLOSE:
        return None
    if resume.get("starting_production_hash") != production_hash or resume.get("starting_research_hash") != research_hash:
        changed = copy.deepcopy(resume)
        changed["status"] = "BASELINE_CHANGED"
        return changed
    return resume


def _persist_waiting_resume(
    path: Path,
    *,
    status: str,
    reason: str,
    external_path: Path,
    external_freeze: dict[str, Any],
    selected: dict[str, Any],
    before_production_hash: str | None,
    before_research_hash: str | None,
    market_fetch_count: int,
    external_fetch_count: int,
) -> None:
    atomic_write_json(
        path,
        {
            "schema": "observation-preparation-resume.v1",
            "status": status,
            "reason": reason,
            "cycle_identity": hashlib.sha256(
                json.dumps(
                    {
                        "production": before_production_hash,
                        "research": before_research_hash,
                        "required_external_time": external_freeze.get("required_external_time"),
                        "target_checkpoint": selected.get("next_required_checkpoint"),
                    },
                    sort_keys=True,
                ).encode("utf-8")
            ).hexdigest(),
            "external_path": str(external_path),
            "external_freeze": external_freeze,
            "required_external_time": external_freeze.get("required_external_time"),
            "target_checkpoint": selected.get("next_required_checkpoint"),
            "selected": selected,
            "starting_production_hash": before_production_hash,
            "starting_research_hash": before_research_hash,
            "market_fetch_count": market_fetch_count,
            "external_fetch_count": external_fetch_count,
        },
    )


def _clear_resume_state(path: Path) -> None:
    if path.exists():
        path.unlink()


def _baseline_changed_result(
    *,
    resume: dict[str, Any],
    state_path: Path,
    research_tracker_path: Path,
    before_production_hash: str | None,
    before_research_hash: str | None,
    loaded_from: str,
    symbols: tuple[str, ...],
) -> dict[str, Any]:
    return {
        "schema": "observation-preparation.v1",
        "status": BLOCKED,
        "reason": "PREPARATION_BASELINE_CHANGED",
        "final": BLOCKED,
        "canonical_checkpoint": None,
        "required_external_time": resume.get("required_external_time"),
        "next_required_checkpoint": resume.get("target_checkpoint"),
        "market_refresh_count": max(0, int(resume.get("market_fetch_count") or 0) - 1),
        "market_fetch_count": int(resume.get("market_fetch_count") or 0),
        "external_fetch_count": int(resume.get("external_fetch_count") or 0),
        "state_loaded_from": loaded_from,
        "previous_review_timestamp": {symbol: 0 for symbol in symbols},
        "market_replay_ready": "NO",
        "external_ready": "NO",
        "liquidation_ready": "NO",
        "research_store_ready": "YES" if before_research_hash is not None else "NO",
        "semantic_classifiers_ready": "NO",
        "state_immutability": {
            "production_before_sha256": before_production_hash,
            "production_after_sha256": _sha256_or_none(state_path),
            "production_unchanged": before_production_hash == _sha256_or_none(state_path),
            "research_before_sha256": before_research_hash,
            "research_after_sha256": _sha256_or_none(research_tracker_path),
            "research_unchanged": before_research_hash == _sha256_or_none(research_tracker_path),
        },
    }

def _result(
    *,
    status: str,
    reason: str | None,
    market_path: Path,
    external_path: Path,
    market_snapshot: dict[str, Any],
    external_freeze: dict[str, Any],
    selected: dict[str, Any],
    state_path: Path,
    research_tracker_path: Path,
    loaded_from: str,
    previous_state: dict[str, dict],
    research_required_starts: dict[str, int],
    before_production_hash: str | None,
    before_research_hash: str | None,
    market_fetch_count: int,
    external_fetch_count: int,
    liquidation_root: Path,
    symbols: tuple[str, ...],
    external_ready: dict[str, Any] | None = None,
    liquidation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    after_production_hash = _sha256_or_none(state_path)
    after_research_hash = _sha256_or_none(research_tracker_path)
    production_unchanged = before_production_hash == _sha256_or_none(state_path)
    research_unchanged = before_research_hash == _sha256_or_none(research_tracker_path)
    market_ready = status == READY or selected.get("status") == READY
    liquidation = liquidation or _empty_liquidation_readiness(liquidation_root, symbols)
    external_ready = external_ready or _external_temporal_ready(external_freeze, selected.get("canonical_checkpoint"))
    return {
        "schema": "observation-preparation.v1",
        "status": status,
        "reason": reason,
        "final": status,
        "canonical_checkpoint": selected.get("canonical_checkpoint"),
        "initial_checkpoint": selected.get("initial_checkpoint"),
        "latest_m5_open": selected.get("latest_m5_open"),
        "required_external_time": external_freeze["required_external_time"],
        "next_required_m5_open": selected.get("next_required_m5_open"),
        "next_required_checkpoint": selected.get("next_required_checkpoint"),
        "external_snapshot_timestamp": external_freeze.get("external_snapshot_timestamp"),
        "market_refresh_count": max(0, market_fetch_count - 1),
        "market_fetch_count": market_fetch_count,
        "external_fetch_count": external_fetch_count,
        "market_path": str(market_path),
        "external_path": str(external_path),
        "state_loaded_from": loaded_from,
        "previous_review_timestamp": {
            symbol: int((previous_state.get(symbol) or {}).get("previous_review_timestamp") or 0)
            for symbol in symbols
        },
        "market_replay_ready": "YES" if market_ready else "NO",
        "external_ready": "YES" if external_ready["ready"] else "NO",
        "liquidation_ready": "YES" if liquidation["ready"] else "NO",
        "research_store_ready": "YES" if before_research_hash is not None else "NO",
        "semantic_classifiers_ready": "YES" if external_ready["ready"] else "NO",
        "TEMPORAL_COORDINATOR_NO_HINDSIGHT": "PASS" if external_ready["no_hindsight"] else "FAIL",
        "external_freeze": external_freeze,
        "external_temporal": external_ready,
        "liquidation": liquidation,
        "research_required_starts": research_required_starts,
        "state_immutability": {
            "production_before_sha256": before_production_hash,
            "production_after_sha256": after_production_hash,
            "production_unchanged": production_unchanged,
            "research_before_sha256": before_research_hash,
            "research_after_sha256": after_research_hash,
            "research_unchanged": research_unchanged,
        },
    }


def _select_market_checkpoint(
    snapshot: dict[str, Any],
    required_external_time: int,
    symbols: tuple[str, ...],
    previous_state: dict[str, dict] | None = None,
) -> dict[str, Any]:
    latest_by_symbol = {symbol: _latest_m5_open(snapshot, symbol) for symbol in symbols}
    missing = [symbol for symbol, timestamp in latest_by_symbol.items() if timestamp is None]
    if missing:
        target_checkpoint = _first_m5_checkpoint_at_or_after(required_external_time)
        return {
            "status": BLOCKED,
            "reason": "MISSING_M5_MARKET_DATA",
            "initial_checkpoint": None,
            "latest_m5_open": latest_by_symbol,
            "next_required_m5_open": target_checkpoint - 300,
            "next_required_checkpoint": target_checkpoint,
        }
    initial_checkpoint = min(int(timestamp or 0) for timestamp in latest_by_symbol.values()) + 300
    if required_external_time <= initial_checkpoint:
        target_checkpoint = initial_checkpoint
    else:
        target_checkpoint = _first_m5_checkpoint_at_or_after(required_external_time)
    target_checkpoint = max(target_checkpoint, _previous_min_next_checkpoint(previous_state or {}, symbols))
    target_open = target_checkpoint - 300
    if all(_has_closed_m5_open(snapshot, symbol, target_open) for symbol in symbols):
        return {
            "status": READY,
            "reason": None,
            "canonical_checkpoint": target_checkpoint,
            "initial_checkpoint": initial_checkpoint,
            "latest_m5_open": latest_by_symbol,
            "next_required_m5_open": target_open,
            "next_required_checkpoint": target_checkpoint,
        }
    if any(int(timestamp or 0) + 300 < target_checkpoint for timestamp in latest_by_symbol.values()):
        return {
            "status": WAITING_FOR_M5_CLOSE,
            "reason": WAITING_FOR_M5_CLOSE,
            "canonical_checkpoint": None,
            "initial_checkpoint": initial_checkpoint,
            "latest_m5_open": latest_by_symbol,
            "next_required_m5_open": target_open,
            "next_required_checkpoint": target_checkpoint,
        }
    return {
        "status": BLOCKED,
        "reason": MARKET_CHECKPOINT_GAP,
        "canonical_checkpoint": None,
        "initial_checkpoint": initial_checkpoint,
        "latest_m5_open": latest_by_symbol,
        "next_required_m5_open": target_open,
        "next_required_checkpoint": target_checkpoint,
    }


def _previous_min_next_checkpoint(previous_state: dict[str, dict], symbols: tuple[str, ...]) -> int:
    previous_opens = [
        _optional_int((previous_state.get(symbol) or {}).get("previous_review_timestamp"))
        for symbol in symbols
    ]
    previous_opens = [timestamp for timestamp in previous_opens if timestamp is not None]
    if not previous_opens:
        return 0
    return max(previous_opens) + 600


def _external_freeze(snapshot: dict[str, Any], symbols: tuple[str, ...]) -> dict[str, Any]:
    fields: dict[str, dict[str, dict[str, Any]]] = {}
    required_times: list[int] = []
    for symbol in symbols:
        raw = ((snapshot.get("symbols") or {}).get(symbol) or {}).get("evidence", {}).get("raw_metrics", {})
        fields[symbol] = {}
        for field in REQUIRED_EXTERNAL_FIELDS:
            metric = copy.deepcopy(raw.get(field))
            fields[symbol][field] = metric
            if isinstance(metric, dict) and metric.get("availability") == "AVAILABLE":
                for key in ("source_timestamp", "available_at"):
                    value = metric.get(key)
                    if value is not None:
                        required_times.append(int(value))
    required_external_time = max(required_times) if required_times else 0
    return {
        "external_snapshot_timestamp": snapshot.get("fetch_timestamp"),
        "fetch_timestamp": snapshot.get("fetch_timestamp"),
        "required_external_time": required_external_time,
        "required_fields": fields,
    }


def _external_temporal_ready(external_freeze: dict[str, Any], checkpoint: Any) -> dict[str, Any]:
    if checkpoint in {None, "NONE"}:
        return {"ready": False, "no_hindsight": True, "offending_fields": []}
    checkpoint = int(checkpoint)
    offending = []
    diagnostics: dict[str, dict[str, dict[str, Any]]] = {}
    for symbol, fields in external_freeze["required_fields"].items():
        diagnostics[symbol] = {}
        for field, metric in fields.items():
            source = _optional_int(metric.get("source_timestamp")) if isinstance(metric, dict) else None
            available = _optional_int(metric.get("available_at")) if isinstance(metric, dict) else None
            availability = metric.get("availability") if isinstance(metric, dict) else "UNAVAILABLE"
            eligible = availability == "AVAILABLE" and source is not None and available is not None and source <= checkpoint and available <= checkpoint
            diagnostics[symbol][field] = {
                "availability": availability,
                "source_timestamp": source,
                "available_at": available,
                "TEMPORALLY_ELIGIBLE": "YES" if eligible else "NO",
            }
            if not eligible:
                offending.append({"symbol": symbol, "field": field, **diagnostics[symbol][field]})
    return {
        "ready": not offending,
        "no_hindsight": not any(
            item.get("source_timestamp", 0) and item["source_timestamp"] > checkpoint
            or item.get("available_at", 0) and item["available_at"] > checkpoint
            for item in offending
        ),
        "offending_fields": offending,
        "fields": diagnostics,
    }


def _liquidation_readiness(root: Path, checkpoint: int, symbols: tuple[str, ...]) -> dict[str, Any]:
    coverage_store = CoverageStore(root)
    event_store = LiquidationEventStore(root)
    symbols_report = {}
    ready = True
    for symbol in symbols:
        coverage = coverage_store.load(symbol)
        aggregates = aggregate_liquidations(event_store.read_events(symbol), coverage, checkpoint)
        windows = {}
        for label in ("5m", "15m", "1h"):
            item = aggregates[label]
            complete = item.get("coverage_status") == "COMPLETE"
            ready = ready and complete
            windows[label] = {
                "coverage_status": item.get("coverage_status"),
                "long_liquidation_notional": item.get("long_liquidation_notional"),
                "short_liquidation_notional": item.get("short_liquidation_notional"),
                "total_liquidation_notional": item.get("total_liquidation_notional"),
                "event_count_long": item.get("event_count_long"),
                "event_count_short": item.get("event_count_short"),
                "availability": "AVAILABLE" if complete else item.get("coverage_status"),
            }
        symbols_report[symbol] = windows
    return {"ready": ready, "symbols": symbols_report}


def _empty_liquidation_readiness(root: Path, symbols: tuple[str, ...]) -> dict[str, Any]:
    return {"ready": False, "symbols": {symbol: {} for symbol in symbols}, "root": str(root)}


def _research_required_starts(path: Path | None) -> dict[str, int]:
    if path is None or not path.exists():
        return {}
    return pending_outcome_required_start_by_symbol(load_tracker_store(path), timeframe="M5")


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _sha256_or_none(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _first_m5_checkpoint_at_or_after(timestamp: int) -> int:
    if timestamp <= 0:
        return 0
    remainder = timestamp % 300
    return timestamp if remainder == 0 else timestamp + (300 - remainder)


def _latest_m5_open(snapshot: dict[str, Any], symbol: str) -> int | None:
    frame = (snapshot.get(symbol) or {}).get("M5") or {}
    value = frame.get("latest_closed_candle_timestamp")
    if value is not None:
        return int(value)
    candles = frame.get("OHLCV") or []
    return int(candles[-1]["timestamp"]) if candles else None


def _has_closed_m5_open(snapshot: dict[str, Any], symbol: str, timestamp: int) -> bool:
    frame = (snapshot.get(symbol) or {}).get("M5") or {}
    current_open = frame.get("current_open_candle_timestamp")
    for candle in frame.get("OHLCV") or []:
        if int(candle.get("timestamp", -1)) == timestamp:
            return current_open is None or int(current_open) != timestamp
    return False


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
