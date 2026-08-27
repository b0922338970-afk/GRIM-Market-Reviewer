"""Live research integration for missed opportunity tracking.

This module is intentionally research-only. It runs after production review,
opportunity evidence, and optional external evidence have already been
finalized. It never mutates production review-state, creates entries, scores,
grades, or trading instructions.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Callable

from .missed_opportunity import (
    DEFAULT_TRACKER_PATH,
    append_snapshot,
    backfill_46_49,
    build_origin_candidate,
    create_tracker,
    empty_store,
    is_eligible_origin,
    load_tracker_store,
    persist_tracker_store,
    record_production_conversion,
    update_horizon_outcomes,
    upsert_tracker,
)
from .model import MarketDataFrame


RESEARCH_SUCCESS = "PASS"
RESEARCH_ERROR = "FAIL"
MISSING_RESEARCH_STORE_STATUS = "INITIALIZED_EMPTY"
RESEARCH_STORE_LOADED_STATUS = "LOADED"


PersistFunc = Callable[[Path, dict[str, Any]], None]


def load_or_initialize_research_store(path: Path = DEFAULT_TRACKER_PATH) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return empty_store(), MISSING_RESEARCH_STORE_STATUS
    return load_tracker_store(path), RESEARCH_STORE_LOADED_STATUS


def apply_missed_opportunity_observation(
    *,
    reviews: dict[str, dict[str, Any]],
    frames: dict[str, dict[str, MarketDataFrame]],
    opportunity_snapshots: dict[str, dict[str, Any]],
    external_evidence: dict[str, dict[str, Any]] | None = None,
    observation_number: int,
    store_path: Path = DEFAULT_TRACKER_PATH,
    persist: bool = True,
    persist_func: PersistFunc = persist_tracker_store,
) -> dict[str, Any]:
    """Apply one already-finalized observation to research trackers.

    Failures are reported as research errors and do not imply production review
    invalidity. Callers should run this after production persistence/reload has
    succeeded.
    """
    try:
        before_store, load_status = load_or_initialize_research_store(store_path)
        store = copy.deepcopy(before_store)
        external_by_symbol = external_evidence or {}
        symbol_reports: dict[str, dict[str, Any]] = {}
        for symbol, review in reviews.items():
            symbol_report = _apply_symbol_observation(
                store=store,
                symbol=symbol,
                review=review,
                frames=frames[symbol],
                opportunity_snapshot=opportunity_snapshots[symbol],
                external_evidence=external_by_symbol.get(symbol),
                observation_number=observation_number,
                preexisting_store=before_store,
            )
            symbol_reports[symbol] = symbol_report
        for record in store.get("records", []):
            symbol = record.get("symbol")
            symbol_frames = frames.get(symbol)
            if symbol_frames and "M5" in symbol_frames:
                update_horizon_outcomes(record, symbol_frames["M5"])
        health = tracker_health(store)
        reload_matches = None
        if persist:
            persist_func(store_path, store)
            reloaded = load_tracker_store(store_path)
            reload_matches = reloaded == store
            if not reload_matches:
                raise ValueError("research reload mismatch after atomic persist")
        return {
            "schema": "missed-opportunity-live-report.v1",
            "research_persistence": RESEARCH_SUCCESS if persist else "DRY_RUN",
            "store_loaded": load_status,
            "store_path": str(store_path),
            "symbols": symbol_reports,
            "health": health,
            "fresh_reload_match": reload_matches,
            "production_observation_impact": "NONE",
        }
    except Exception as exc:  # noqa: BLE001 - research layer must fail closed without production impact.
        return {
            "schema": "missed-opportunity-live-report.v1",
            "research_persistence": RESEARCH_ERROR,
            "store_path": str(store_path),
            "error": exc.__class__.__name__,
            "message": str(exc),
            "production_observation_impact": "NONE",
        }


def dry_run_missed_opportunity_observation(
    *,
    store: dict[str, Any],
    reviews: dict[str, dict[str, Any]],
    frames: dict[str, dict[str, MarketDataFrame]],
    opportunity_snapshots: dict[str, dict[str, Any]],
    external_evidence: dict[str, dict[str, Any]] | None,
    observation_number: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    working = copy.deepcopy(store)
    symbols = {}
    for symbol, review in reviews.items():
        symbols[symbol] = _apply_symbol_observation(
            store=working,
            symbol=symbol,
            review=review,
            frames=frames[symbol],
            opportunity_snapshot=opportunity_snapshots[symbol],
            external_evidence=(external_evidence or {}).get(symbol),
            observation_number=observation_number,
            preexisting_store=store,
        )
    for record in working.get("records", []):
        symbol_frames = frames.get(record.get("symbol"))
        if symbol_frames and "M5" in symbol_frames:
            update_horizon_outcomes(record, symbol_frames["M5"])
    return working, {"symbols": symbols, "health": tracker_health(working), "research_persistence": "DRY_RUN"}


def explicit_backfill_v426(store_path: Path = DEFAULT_TRACKER_PATH, persist_func: PersistFunc = persist_tracker_store) -> dict[str, Any]:
    """Explicit, idempotent migration for validated #46-#49 trackers."""
    existing, load_status = load_or_initialize_research_store(store_path)
    seeded = backfill_46_49()
    merged = copy.deepcopy(existing)
    existing_ids = {record.get("tracker_id") for record in merged.get("records", [])}
    for record in seeded.get("records", []):
        if record.get("tracker_id") not in existing_ids:
            merged.setdefault("records", []).append(copy.deepcopy(record))
            existing_ids.add(record.get("tracker_id"))
    persist_func(store_path, merged)
    reloaded = load_tracker_store(store_path)
    return {
        "schema": "missed-opportunity-backfill-report.v1",
        "store_loaded": load_status,
        "store_path": str(store_path),
        "research_persistence": RESEARCH_SUCCESS,
        "record_count": len(reloaded.get("records", [])),
        "added_records": len(merged.get("records", [])) - len(existing.get("records", [])),
        "fresh_reload_match": reloaded == merged,
    }


def missed_opportunity_status(path: Path = DEFAULT_TRACKER_PATH) -> dict[str, Any]:
    store, load_status = load_or_initialize_research_store(path)
    records = []
    for record in store.get("records", []):
        snapshots = record.get("snapshots", [])
        latest = snapshots[-1] if snapshots else None
        records.append(
            {
                "symbol": record.get("symbol"),
                "tracker_id": record.get("tracker_id"),
                "status": record.get("status"),
                "origin": {
                    "observation": record.get("origin_observation"),
                    "timestamp": record.get("origin_snapshot_timestamp"),
                    "price": record.get("origin_price"),
                },
                "latest_snapshot": None if latest is None else {
                    "observation": latest.get("observation_number"),
                    "timestamp": latest.get("snapshot_timestamp"),
                    "price": latest.get("price"),
                    "trajectory": latest.get("trajectory_state"),
                },
                "conversion": {
                    "converted_to_production": record.get("converted_to_production"),
                    "production_sequence_id": record.get("production_sequence_id"),
                    "conversion_timestamp": record.get("conversion_timestamp"),
                },
                "horizon_statuses": {
                    horizon: outcome.get("horizon_status")
                    for horizon, outcome in (record.get("outcomes") or {}).items()
                },
            }
        )
    return {
        "schema": store.get("schema"),
        "store_loaded": load_status,
        "store_path": str(path),
        "record_count": len(store.get("records", [])),
        "records": records,
        "health": tracker_health(store),
    }


def tracker_health(store: dict[str, Any]) -> dict[str, Any]:
    records = store.get("records", [])
    pending = 0
    complete = 0
    for record in records:
        for outcome in (record.get("outcomes") or {}).values():
            if outcome.get("horizon_status") == "COMPLETE":
                complete += 1
            else:
                pending += 1
    return {
        "store_loaded": True,
        "active_records": sum(1 for item in records if item.get("status") in {"ACTIVE", "DETERIORATING"}),
        "terminal_records": sum(1 for item in records if item.get("status") in {"TERMINAL", "CONVERTED", "OUTCOME_COMPLETE"}),
        "pending_outcomes": pending,
        "complete_outcomes": complete,
        "persistence_status": "NOT_ATTEMPTED",
    }


def build_tracker_candidate_from_observation(
    *,
    symbol: str,
    review: dict[str, Any],
    frames: dict[str, MarketDataFrame],
    opportunity_snapshot: dict[str, Any],
    external_evidence: dict[str, Any] | None,
    observation_number: int,
) -> dict[str, Any]:
    direction = _direction_from_review(review)
    snapshot_timestamp = int(opportunity_snapshot.get("snapshot_timestamp") or _latest_closed(frames))
    price = frames["M5"].closed_candles()[-1].close
    evidence = opportunity_evidence_from_snapshot(opportunity_snapshot, external_evidence)
    return build_origin_candidate(
        symbol=symbol,
        direction=direction,
        observation_number=observation_number,
        snapshot_timestamp=snapshot_timestamp,
        price=price,
        production_sequence_id=str(review.get("Sequence_ID") or opportunity_snapshot.get("sequence_id") or "NONE"),
        production_sequence_state=str(review.get("Sequence_State") or opportunity_snapshot.get("sequence_state") or "UNKNOWN"),
        production_review_state=str(review.get("State") or "UNKNOWN"),
        opportunity_evidence=evidence,
        external_evidence=external_tracker_fields(external_evidence),
        risk_signatures=list(opportunity_snapshot.get("risk_signatures") or []),
        new_legal_genesis_active=_new_legal_genesis_in_review(review),
    )


def opportunity_evidence_from_snapshot(opportunity_snapshot: dict[str, Any], external_evidence: dict[str, Any] | None = None) -> dict[str, str]:
    raw = opportunity_snapshot.get("raw_metrics") or {}
    features = opportunity_snapshot.get("features") or {}
    truth = opportunity_snapshot.get("truth") or {}
    risk = set(opportunity_snapshot.get("risk_signatures") or [])
    external_domains = (external_evidence or {}).get("domain_classification") or {}
    htf_alignment = _nested(features, "HTF_ALIGNMENT", "value", "aligned_count")
    displacement_status = _nested(features, "DISPLACEMENT_STRENGTH", "value", "status")
    trend_maturity = _nested(features, "TREND_MATURITY", "value", "trend_maturity")
    active_sweep = truth.get("active_sweep")
    return {
        "STRUCTURE": "POSITIVE" if isinstance(htf_alignment, int) and htf_alignment > 0 else "NEUTRAL",
        "LIQUIDITY": "POSITIVE" if active_sweep or _available(raw, "LIQUIDITY", "liquidity_distance_pct") else _external_class(external_domains, "LIQUIDATION_CONTEXT", default="NEUTRAL"),
        "MOMENTUM": "POSITIVE" if displacement_status == "AVAILABLE" or trend_maturity in {"MATURE_CONTINUATION", "ALIGNED_PULLBACK"} else "NEUTRAL",
        "LOCATION": "NEGATIVE" if "RANGE_MIDPOINT" in risk else "NEUTRAL",
        "FRESHNESS": str(_nested(features, "POI_FRESHNESS", "value", "status") or "DATA_UNAVAILABLE"),
        "REMAINING_OPPORTUNITY": "NEGATIVE" if "OPPORTUNITY_TOO_EXTENDED" in risk or "POOR_REMAINING_ROOM" in risk else "NEUTRAL",
        "POSITIONING": _external_class(external_domains, "POSITIONING", default="DATA_UNAVAILABLE"),
        "CROWDING": _external_class(external_domains, "CROWDING", default="DATA_UNAVAILABLE"),
        "LIQUIDATION_CONTEXT": _external_class(external_domains, "LIQUIDATION_CONTEXT", default="DATA_UNAVAILABLE"),
        "REGIME": str(_nested(features, "REGIME", "value", "regime") or opportunity_snapshot.get("sequence_state") or "UNAVAILABLE"),
        "SWING_BIAS": str(truth.get("swing_bias") or "UNAVAILABLE"),
    }


def external_tracker_fields(external_evidence: dict[str, Any] | None) -> dict[str, Any]:
    metrics = (external_evidence or {}).get("raw_metrics") or {}
    return {
        "oi_current": _metric_value(metrics, "oi"),
        "oi_delta_5m": _metric_value(metrics, "oi_change_5m"),
        "oi_delta_15m": _metric_value(metrics, "oi_change_15m"),
        "oi_delta_1h": _metric_value(metrics, "oi_change_1h"),
        "oi_delta_4h": _metric_value(metrics, "oi_change_4h"),
        "funding_current": _metric_value(metrics, "funding_rate"),
        "funding_percentile": _metric_value(metrics, "funding_percentile"),
        "funding_change": _metric_value(metrics, "funding_change"),
        "liquidation_5m": _liquidation_window(metrics, "5m"),
        "liquidation_15m": _liquidation_window(metrics, "15m"),
        "liquidation_1h": _liquidation_window(metrics, "1h"),
        "source_evidence_ids": sorted(
            str(item.get("evidence_id"))
            for item in metrics.values()
            if isinstance(item, dict) and item.get("availability") == "AVAILABLE" and item.get("evidence_id")
        ),
        "provenance": "current observation external evidence" if external_evidence else "external evidence unavailable for current observation",
    }


def _apply_symbol_observation(
    *,
    store: dict[str, Any],
    symbol: str,
    review: dict[str, Any],
    frames: dict[str, MarketDataFrame],
    opportunity_snapshot: dict[str, Any],
    external_evidence: dict[str, Any] | None,
    observation_number: int,
    preexisting_store: dict[str, Any],
) -> dict[str, Any]:
    before = _matching_record(preexisting_store, symbol)
    candidate = build_tracker_candidate_from_observation(
        symbol=symbol,
        review=review,
        frames=frames,
        opportunity_snapshot=opportunity_snapshot,
        external_evidence=external_evidence,
        observation_number=observation_number,
    )
    conversion = False
    if before and _new_legal_genesis_in_review(review) and _direction_from_review(review) == before.get("direction"):
        current = _matching_record(store, symbol, before.get("tracker_id"))
        if current and not current.get("converted_to_production"):
            record_production_conversion(
                current,
                str(review.get("Sequence_ID") or "UNKNOWN"),
                _new_legal_genesis_timestamp(review) or int(candidate["snapshot_timestamp"]),
                float(candidate["price"]),
            )
            conversion = True
    before_record = _matching_record(store, symbol)
    before_snapshot_count = len(before_record.get("snapshots", [])) if before_record else 0
    if before_record and _starts_new_research_trajectory(before_record, candidate):
        store.setdefault("records", []).append(create_tracker(candidate, created_at=None))
    elif before_record and before_record.get("status") in {"ACTIVE", "DETERIORATING"}:
        append_snapshot(before_record, candidate, updated_at=None)
    else:
        upsert_tracker(store, candidate, updated_at=None)
    after_record = _matching_record(store, symbol)
    after_snapshot_count = len(after_record.get("snapshots", [])) if after_record else 0
    latest = after_record.get("snapshots", [])[-1] if after_record and after_record.get("snapshots") else None
    return {
        "tracker_active": "YES" if after_record and after_record.get("status") in {"ACTIVE", "DETERIORATING", "CONVERTED"} else "NO",
        "tracker_id": after_record.get("tracker_id") if after_record else None,
        "tracker_status": after_record.get("status") if after_record else None,
        "origin_observation": after_record.get("origin_observation") if after_record else None,
        "origin_timestamp": after_record.get("origin_snapshot_timestamp") if after_record else None,
        "origin_price": after_record.get("origin_price") if after_record else None,
        "current_snapshot_added": "YES" if after_snapshot_count > before_snapshot_count else "NO",
        "snapshot_count": after_snapshot_count,
        "trajectory_state": latest.get("trajectory_state") if latest else None,
        "current_price": candidate["price"],
        "price_change_from_origin_pct": latest.get("price_change_from_origin_pct") if latest else None,
        "MFE_MAE": after_record.get("outcomes") if after_record else None,
        "production_conversion": "YES" if conversion else "NO",
        "relation_features": latest.get("relation_features") if latest else [],
        "eligible_origin": candidate.get("new_legal_genesis_active") is False,
    }


def _matching_record(store: dict[str, Any], symbol: str, tracker_id: Any | None = None) -> dict[str, Any] | None:
    records = [record for record in store.get("records", []) if record.get("symbol") == symbol]
    if tracker_id is not None:
        records = [record for record in records if record.get("tracker_id") == tracker_id]
    if not records:
        return None
    active = [record for record in records if record.get("status") in {"ACTIVE", "DETERIORATING", "CONVERTED"}]
    return active[-1] if active else records[-1]


def _starts_new_research_trajectory(record: dict[str, Any], candidate: dict[str, Any]) -> bool:
    if record.get("status") != "DETERIORATING":
        return False
    if not is_eligible_origin(candidate):
        return False
    if candidate.get("new_legal_genesis_active") is True:
        return False
    same_sequence = str(record.get("production_sequence_id_at_origin") or "") == str(candidate.get("production_sequence_id") or "")
    if same_sequence:
        return False
    return _trajectory_context_key(record.get("context_signature") or {}) != _trajectory_context_key(candidate.get("context_signature") or {})


def _trajectory_context_key(context: dict[str, Any]) -> tuple[Any, ...]:
    return (
        context.get("trend_regime"),
        context.get("swing_bias"),
        context.get("structure_class"),
        context.get("momentum_class"),
    )

def _new_legal_genesis_in_review(review: dict[str, Any]) -> bool:
    return _new_legal_genesis_timestamp(review) is not None


def _new_legal_genesis_timestamp(review: dict[str, Any]) -> int | None:
    for transition in review.get("Sequence_Transitions") or []:
        if not isinstance(transition, dict):
            continue
        if transition.get("previous_state") == "NONE" and transition.get("new_state") == "SEEKING_LIQUIDITY":
            try:
                return int(transition.get("timestamp"))
            except (TypeError, ValueError):
                return None
    return None


def _direction_from_review(review: dict[str, Any]) -> str:
    bias = str(review.get("Swing_Bias") or "").upper()
    if bias == "BEARISH":
        return "SHORT"
    return "LONG"


def _latest_closed(frames: dict[str, MarketDataFrame]) -> int:
    return max(frame.latest_closed_candle_timestamp for frame in frames.values())


def _nested(mapping: dict[str, Any], *keys: str) -> Any:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _available(raw: dict[str, Any], domain: str, key: str) -> bool:
    value = _nested(raw, domain, key)
    return value is not None


def _external_class(domains: dict[str, Any], domain: str, default: str) -> str:
    value = _nested(domains, domain, "classification")
    return str(value or default)


def _metric_value(metrics: dict[str, Any], metric_id: str) -> Any:
    item = metrics.get(metric_id)
    if not isinstance(item, dict) or item.get("availability") != "AVAILABLE":
        return None
    return item.get("value")


def _metric_availability(metrics: dict[str, Any], metric_id: str) -> str:
    item = metrics.get(metric_id)
    if not isinstance(item, dict):
        return "UNAVAILABLE"
    return str(item.get("availability") or "UNAVAILABLE")


def _liquidation_window(metrics: dict[str, Any], window: str) -> dict[str, Any]:
    long_key = f"long_liquidation_notional_{window}"
    short_key = f"short_liquidation_notional_{window}"
    long_value = _metric_value(metrics, long_key)
    short_value = _metric_value(metrics, short_key)
    return {
        "long": long_value,
        "short": short_value,
        "total": None if long_value is None or short_value is None else long_value + short_value,
        "imbalance": None if long_value is None or short_value is None or long_value + short_value == 0 else (long_value - short_value) / (long_value + short_value),
        "coverage_status": "AVAILABLE" if _metric_availability(metrics, long_key) == "AVAILABLE" and _metric_availability(metrics, short_key) == "AVAILABLE" else "UNAVAILABLE",
    }
