"""Research-only missed opportunity tracker.

The tracker intentionally lives outside production review-state persistence.
It records positive market context that did not become a legal production
execution sequence, then measures forward outcomes without creating entries,
scores, grades, or lifecycle transitions.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .model import Candle, MarketDataFrame, TIMEFRAME_SECONDS
from .persistence import atomic_write_json


TRACKER_SCHEMA = "missed-opportunity-tracker.v1"
RESEARCH_WATERMARK_SCHEMA = "research-observation-watermark.v1"
RESEARCH_WATERMARK_KEY = "research_observation_watermark"
DEFAULT_TRACKER_PATH = Path("research/missed-opportunities.json")
TRACKER_STATUSES = {"ACTIVE", "DETERIORATING", "CONVERTED", "TERMINAL", "OUTCOME_COMPLETE"}
EPISODE_STATUSES = {"OPEN", "BROKEN", "CLOSED"}
TRAJECTORY_STATES = {"IMPROVING", "STABLE", "DETERIORATING"}
HORIZON_SECONDS = {
    "1H": 3_600,
    "4H": 14_400,
    "12H": 43_200,
    "24H": 86_400,
}
TERMINAL_PRODUCTION_STATES = {"EXPIRED_NO_TRIGGER", "INVALIDATED"}
RESEARCH_RELATION_LABELS = {
    "PRICE_OI_BUILD",
    "PRICE_UP_OI_UP",
    "PRICE_UP_OI_DOWN",
    "PRICE_DOWN_OI_UP",
    "PRICE_DOWN_OI_DOWN",
    "PRICE_FLAT_OI_UP",
    "PRICE_FLAT_OI_DOWN",
    "PRICE_OI_NEUTRAL",
    "PRICE_OI_DIVERGENCE",
    "NON_LIQUIDATION_DELEVERAGING",
    "CROWDED_POSITION_BUILD",
    "POSITIONING_REBUILD",
    "ZERO_LIQUIDATION_CONTEXT",
}
FORBIDDEN_EXECUTION_KEYS = {"entry", "sl", "tp", "score", "grade", "buy_now", "sell_now"}


def empty_store(generated_at: str | None = None) -> dict[str, Any]:
    return {
        "schema": TRACKER_SCHEMA,
        "generated_at": generated_at or _utc_now(),
        "records": [],
    }


def load_tracker_store(path: Path = DEFAULT_TRACKER_PATH) -> dict[str, Any]:
    if not path.exists():
        return empty_store()
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != TRACKER_SCHEMA or not isinstance(data.get("records"), list):
        raise ValueError("unsupported missed opportunity tracker schema")
    return _with_episode_defaults(data)


def persist_tracker_store(path: Path, store: dict[str, Any]) -> None:
    _validate_store_is_research_only(store)
    atomic_write_json(path, store)



def latest_tracker_snapshot_observation(store: dict[str, Any]) -> int:
    latest = 0
    for record in store.get("records", []):
        if not isinstance(record, dict):
            continue
        for snapshot in record.get("snapshots", []):
            if not isinstance(snapshot, dict):
                continue
            value = _optional_int(snapshot.get("observation_number"))
            if value:
                latest = max(latest, value)
    return latest


def research_watermark(store: dict[str, Any]) -> dict[str, Any]:
    watermark = store.get(RESEARCH_WATERMARK_KEY)
    if isinstance(watermark, dict) and watermark.get("schema") == RESEARCH_WATERMARK_SCHEMA:
        return dict(watermark)
    latest_snapshot = latest_tracker_snapshot_observation(store)
    return {
        "schema": RESEARCH_WATERMARK_SCHEMA,
        "latest_processed_observation": latest_snapshot,
        "latest_tracker_snapshot_observation": latest_snapshot,
        "research_result": "BOOTSTRAPPED_FROM_TRACKER_HISTORY",
        "reason": "LEGACY_STORE_WITHOUT_EXPLICIT_WATERMARK",
    }


def latest_processed_observation(store: dict[str, Any]) -> int:
    watermark = research_watermark(store)
    return _optional_int(watermark.get("latest_processed_observation")) or 0


def mark_research_observation_processed(
    store: dict[str, Any],
    *,
    observation_number: int,
    canonical_checkpoint: int | None,
    production_state_sha256: str | None,
    processed_at: int | None,
    research_result: str,
    reason: str | None = None,
    opportunity_snapshots: dict[str, Any] | None = None,
    evidence_provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if observation_number <= 0:
        raise ValueError("observation_number must be positive")
    if canonical_checkpoint is not None and canonical_checkpoint <= 0:
        raise ValueError("canonical_checkpoint must be positive")
    if not production_state_sha256:
        raise ValueError("production_state_sha256 is required")
    existing = store.get(RESEARCH_WATERMARK_KEY)
    if isinstance(existing, dict):
        existing_observation = _optional_int(existing.get("latest_processed_observation")) or 0
        if observation_number < existing_observation:
            raise ValueError("research watermark cannot move backward")
        if observation_number == existing_observation:
            existing_checkpoint = _optional_int(existing.get("canonical_checkpoint"))
            existing_hash = existing.get("production_state_sha256")
            if existing_checkpoint and canonical_checkpoint and existing_checkpoint != canonical_checkpoint:
                raise ValueError("research watermark checkpoint mismatch")
            if existing_hash and production_state_sha256 and existing_hash != production_state_sha256:
                raise ValueError("research watermark production hash mismatch")
    latest_snapshot = latest_tracker_snapshot_observation(store)
    identities: dict[str, Any] = {}
    for symbol, snapshot in (opportunity_snapshots or {}).items():
        if not isinstance(snapshot, dict):
            continue
        identities[str(symbol)] = {
            "opportunity_id": snapshot.get("opportunity_id"),
            "sequence_id": snapshot.get("sequence_id"),
            "sequence_state": snapshot.get("sequence_state"),
            "snapshot_timestamp": snapshot.get("snapshot_timestamp"),
            "opportunity_status": snapshot.get("opportunity_status"),
        }
    store[RESEARCH_WATERMARK_KEY] = {
        "schema": RESEARCH_WATERMARK_SCHEMA,
        "latest_processed_observation": int(observation_number),
        "latest_tracker_snapshot_observation": int(latest_snapshot),
        "canonical_checkpoint": canonical_checkpoint,
        "production_state_sha256": production_state_sha256,
        "processed_at": processed_at,
        "research_result": research_result,
        "reason": reason,
        "opportunity_identities": identities,
        "evidence_provenance": dict(evidence_provenance or {}),
    }
    return store

def deterministic_tracker_id(symbol: str, direction: str, origin_snapshot_timestamp: int, context_signature: dict[str, Any]) -> str:
    payload = json.dumps(_canonical_context(context_signature), sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"{symbol}-{direction}-MOT-{int(origin_snapshot_timestamp)}-{digest}"


def context_hash(context_signature: dict[str, Any]) -> str:
    payload = json.dumps(_canonical_context(context_signature), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_eligible_origin(candidate: dict[str, Any]) -> bool:
    production_terminal = (
        candidate.get("opportunity_status") == "NO_ACTIVE_OPPORTUNITY"
        or candidate.get("production_sequence_state") in TERMINAL_PRODUCTION_STATES
    )
    if not production_terminal or candidate.get("new_legal_genesis_active") is True:
        return False
    evidence = candidate.get("opportunity_evidence") or {}
    if evidence.get("STRUCTURE") != "POSITIVE":
        return False
    if evidence.get("MOMENTUM") != "POSITIVE":
        return False
    if evidence.get("POSITIONING") != "POSITIVE" and evidence.get("LIQUIDITY") != "POSITIVE":
        return False
    risks = set(candidate.get("risk_signatures") or [])
    return "HARD_RESEARCH_INVALIDATION" not in risks


def build_origin_candidate(
    *,
    symbol: str,
    direction: str,
    observation_number: int,
    snapshot_timestamp: int,
    price: float,
    production_sequence_id: str,
    production_sequence_state: str,
    production_review_state: str,
    opportunity_evidence: dict[str, str],
    external_evidence: dict[str, Any] | None = None,
    risk_signatures: list[str] | None = None,
    new_legal_genesis_active: bool = False,
) -> dict[str, Any]:
    context = context_signature_from_evidence(opportunity_evidence)
    return {
        "symbol": symbol,
        "direction": direction,
        "observation_number": observation_number,
        "snapshot_timestamp": int(snapshot_timestamp),
        "available_at": int(snapshot_timestamp),
        "price": float(price),
        "production_sequence_id": production_sequence_id,
        "production_sequence_state": production_sequence_state,
        "production_review_state": production_review_state,
        "opportunity_status": "NO_ACTIVE_OPPORTUNITY" if production_sequence_state in TERMINAL_PRODUCTION_STATES else "ACTIVE_OPPORTUNITY",
        "new_legal_genesis_active": new_legal_genesis_active,
        "opportunity_evidence": dict(opportunity_evidence),
        "context_signature": context,
        "external_evidence": _historical_external(external_evidence),
        "risk_signatures": list(risk_signatures or []),
    }


def context_signature_from_evidence(opportunity_evidence: dict[str, str]) -> dict[str, Any]:
    return {
        "trend_regime": opportunity_evidence.get("REGIME", "UNAVAILABLE"),
        "swing_bias": opportunity_evidence.get("SWING_BIAS", "UNAVAILABLE"),
        "structure_class": opportunity_evidence.get("STRUCTURE", "DATA_UNAVAILABLE"),
        "momentum_class": opportunity_evidence.get("MOMENTUM", "DATA_UNAVAILABLE"),
        "positioning_class": opportunity_evidence.get("POSITIONING", "DATA_UNAVAILABLE"),
        "crowding_class": opportunity_evidence.get("CROWDING", "DATA_UNAVAILABLE"),
        "liquidation_class": opportunity_evidence.get("LIQUIDATION_CONTEXT", "DATA_UNAVAILABLE"),
        "location_class": opportunity_evidence.get("LOCATION", "DATA_UNAVAILABLE"),
        "remaining_opportunity_class": opportunity_evidence.get("REMAINING_OPPORTUNITY", "DATA_UNAVAILABLE"),
        "liquidity_class": opportunity_evidence.get("LIQUIDITY", "DATA_UNAVAILABLE"),
    }


def create_tracker(origin: dict[str, Any], created_at: str | None = None) -> dict[str, Any]:
    if not is_eligible_origin(origin):
        raise ValueError("origin contract not satisfied")
    tracker_id = deterministic_tracker_id(origin["symbol"], origin["direction"], origin["snapshot_timestamp"], origin["context_signature"])
    snapshot = _snapshot_from_candidate(origin, origin["price"], "STABLE")
    return {
        "schema": TRACKER_SCHEMA,
        "tracker_id": tracker_id,
        "symbol": origin["symbol"],
        "direction": origin["direction"],
        "status": "ACTIVE",
        "origin_observation": origin["observation_number"],
        "origin_snapshot_timestamp": origin["snapshot_timestamp"],
        "origin_price": origin["price"],
        "production_sequence_id_at_origin": origin["production_sequence_id"],
        "production_sequence_state_at_origin": origin["production_sequence_state"],
        "production_review_state_at_origin": origin["production_review_state"],
        "context_signature": deepcopy(origin["context_signature"]),
        "context_signature_hash": context_hash(origin["context_signature"]),
        "snapshots": [snapshot],
        "outcomes": _empty_outcomes(origin["snapshot_timestamp"]),
        "terminal_reason": None,
        "episode_status": "OPEN",
        "context_break_observation": None,
        "context_break_timestamp": None,
        "context_break_reason": None,
        "context_break_reasons": [],
        "converted_to_production": False,
        "production_sequence_id": None,
        "conversion_timestamp": None,
        "time_from_tracker_origin_to_production_genesis": None,
        "price_change_before_conversion_pct": None,
        "created_at": created_at or _utc_now(),
        "updated_at": created_at or _utc_now(),
    }


def upsert_tracker(store: dict[str, Any], candidate: dict[str, Any], updated_at: str | None = None) -> dict[str, Any]:
    if store.get("schema") != TRACKER_SCHEMA:
        raise ValueError("unsupported missed opportunity tracker schema")
    tracker_id = deterministic_tracker_id(candidate["symbol"], candidate["direction"], candidate["snapshot_timestamp"], candidate["context_signature"])
    existing = next((record for record in store["records"] if record.get("tracker_id") == tracker_id and _episode_status(record) == "OPEN"), None)
    if existing is None:
        candidate_context_hash = context_hash(candidate["context_signature"])
        existing = next(
            (
                record for record in store["records"]
                if record.get("symbol") == candidate["symbol"]
                and record.get("direction") == candidate["direction"]
                and record.get("context_signature_hash") == candidate_context_hash
                and _episode_status(record) == "OPEN"
                and record.get("status") in {"ACTIVE", "DETERIORATING"}
            ),
            None,
        )
    if existing is None:
        if not is_eligible_origin(candidate):
            return store
        store["records"].append(create_tracker(candidate, created_at=updated_at))
        return store
    append_snapshot(existing, candidate, updated_at=updated_at)
    return store


def append_snapshot(record: dict[str, Any], candidate: dict[str, Any], updated_at: str | None = None) -> dict[str, Any]:
    key = (candidate["observation_number"], candidate["snapshot_timestamp"])
    existing = {
        (snapshot.get("observation_number"), snapshot.get("snapshot_timestamp"))
        for snapshot in record.get("snapshots", [])
    }
    if key in existing:
        return record
    origin_price = float(record["origin_price"])
    previous_price = float(record["snapshots"][-1]["price"])
    current_price = float(candidate["price"])
    trajectory_state = _trajectory_state(record["direction"], origin_price, previous_price, current_price)
    record["snapshots"].append(_snapshot_from_candidate(candidate, origin_price, trajectory_state, previous_price=previous_price))
    record["status"] = "DETERIORATING" if trajectory_state == "DETERIORATING" else record.get("status", "ACTIVE")
    if record["status"] not in {"CONVERTED", "TERMINAL", "OUTCOME_COMPLETE"} and trajectory_state != "DETERIORATING":
        record["status"] = "ACTIVE"
    record["updated_at"] = updated_at or _utc_now()
    return record


def record_production_conversion(record: dict[str, Any], sequence_id: str, conversion_timestamp: int, conversion_price: float) -> dict[str, Any]:
    record["converted_to_production"] = True
    record["production_sequence_id"] = sequence_id
    record["conversion_timestamp"] = int(conversion_timestamp)
    record["time_from_tracker_origin_to_production_genesis"] = int(conversion_timestamp) - int(record["origin_snapshot_timestamp"])
    record["price_change_before_conversion_pct"] = _pct_change(record["direction"], float(record["origin_price"]), float(conversion_price))
    record["status"] = "CONVERTED"
    record["terminal_reason"] = "NEW_PRODUCTION_SEQUENCE_STARTED"
    record["episode_status"] = "CLOSED"
    record["context_break_reason"] = "PRODUCTION_CONVERSION"
    record["context_break_reasons"] = ["PRODUCTION_CONVERSION"]
    record["context_break_timestamp"] = int(conversion_timestamp)
    return record


def terminalize_tracker(record: dict[str, Any], reason: str) -> dict[str, Any]:
    if reason not in {"CONTEXT_INVALIDATED", "NEW_PRODUCTION_SEQUENCE_STARTED", "MAX_HORIZON_COMPLETE", "OPPORTUNITY_DECAYED", "MANUAL_RESEARCH_CLOSE"}:
        raise ValueError(f"invalid terminal reason: {reason}")
    record["status"] = "TERMINAL"
    record["terminal_reason"] = reason
    record["episode_status"] = "CLOSED"
    return record


def update_horizon_outcomes(record: dict[str, Any], frame: MarketDataFrame, horizons: tuple[str, ...] = ("1H", "4H", "12H", "24H")) -> dict[str, Any]:
    for horizon in horizons:
        existing = (record.get("outcomes") or {}).get(horizon)
        if isinstance(existing, dict) and existing.get("horizon_status") == "COMPLETE":
            continue
        record["outcomes"][horizon] = calculate_horizon(
            direction=record["direction"],
            origin_timestamp=int(record["origin_snapshot_timestamp"]),
            origin_price=float(record["origin_price"]),
            frame=frame,
            horizon=horizon,
        )
    return record


def calculate_horizon(
    *,
    direction: str,
    origin_timestamp: int,
    origin_price: float,
    frame: MarketDataFrame,
    horizon: str,
) -> dict[str, Any]:
    if horizon not in HORIZON_SECONDS:
        raise ValueError(f"unsupported horizon: {horizon}")
    window_end = int(origin_timestamp) + HORIZON_SECONDS[horizon]
    base = {
        "horizon_status": "PENDING",
        "reference_timestamp": int(origin_timestamp),
        "window_end_timestamp": window_end,
        "MFE_pct": None,
        "MAE_pct": None,
        "MFE_price": None,
        "MAE_price": None,
        "time_to_MFE": None,
        "time_to_MAE": None,
        "price_field": "closed candle high/low",
        "outcome_data_start": None,
        "outcome_data_end": None,
        "outcome_coverage_complete": False,
        "outcome_updated_at": frame.latest_closed_candle_timestamp,
        "outcome_source": "historical_outcome_recovery",
    }
    if frame.latest_closed_candle_timestamp < window_end:
        return base
    candles = [
        candle for candle in frame.closed_candles()
        if int(origin_timestamp) < candle.timestamp <= window_end
    ]
    if not candles:
        base["horizon_status"] = "DATA_GAP"
        return base
    base["outcome_data_start"] = candles[0].timestamp
    base["outcome_data_end"] = candles[-1].timestamp
    expected = HORIZON_SECONDS[horizon] // TIMEFRAME_SECONDS.get(frame.timeframe, 300)
    if len(candles) < expected:
        base["horizon_status"] = "DATA_GAP"
        return base
    excursion = calculate_excursion(direction, origin_price, candles)
    return {**base, "horizon_status": "COMPLETE", "outcome_coverage_complete": True, **excursion}


def calculate_excursion(direction: str, origin_price: float, candles: list[Candle]) -> dict[str, Any]:
    if direction == "LONG":
        mfe_candle = max(candles, key=lambda candle: candle.high)
        mae_candle = min(candles, key=lambda candle: candle.low)
        mfe_price = mfe_candle.high
        mae_price = mae_candle.low
        mfe_pct = (mfe_price / origin_price - 1) * 100
        mae_pct = (mae_price / origin_price - 1) * 100
    elif direction == "SHORT":
        mfe_candle = min(candles, key=lambda candle: candle.low)
        mae_candle = max(candles, key=lambda candle: candle.high)
        mfe_price = mfe_candle.low
        mae_price = mae_candle.high
        mfe_pct = (origin_price / mfe_price - 1) * 100
        mae_pct = (origin_price / mae_price - 1) * 100
    else:
        raise ValueError(f"unsupported direction: {direction}")
    return {
        "MFE_pct": mfe_pct,
        "MAE_pct": mae_pct,
        "MFE_price": mfe_price,
        "MAE_price": mae_price,
        "time_to_MFE": mfe_candle.timestamp,
        "time_to_MAE": mae_candle.timestamp,
    }


def backfill_46_49(generated_at: str | None = None) -> dict[str, Any]:
    store = empty_store(generated_at=generated_at)
    btc_prices = [(46, 1787827800, 79590.12), (47, 1787841900, 80099.75), (48, 1787844900, 80437.92), (49, 1787850000, 80335.99)]
    eth_prices = [(46, 1787827800, 2505.37), (47, 1787841900, 2513.54), (48, 1787844900, 2527.80), (49, 1787850000, 2520.19)]
    for symbol, prices, sequence_id in (("BTC", btc_prices, "BTC-seq-0009"), ("ETH", eth_prices, "ETH-seq-0001")):
        origin = _backfill_candidate(symbol, prices[0], sequence_id)
        store = upsert_tracker(store, origin, updated_at=generated_at)
        tracker = store["records"][-1]
        for item in prices[1:]:
            append_snapshot(tracker, _backfill_candidate(symbol, item, sequence_id), updated_at=generated_at)
    return store


def _backfill_candidate(symbol: str, price_item: tuple[int, int, float], sequence_id: str) -> dict[str, Any]:
    observation_number, timestamp, price = price_item
    evidence = {
        "STRUCTURE": "POSITIVE",
        "LIQUIDITY": "NEUTRAL",
        "MOMENTUM": "POSITIVE" if observation_number in {46, 47, 48} else "NEUTRAL",
        "LOCATION": "NEGATIVE",
        "FRESHNESS": "NOT_APPLICABLE",
        "REMAINING_OPPORTUNITY": "NEGATIVE",
        "POSITIONING": "POSITIVE" if observation_number in {46, 47, 48} else "NEUTRAL",
        "CROWDING": "NEUTRAL",
        "LIQUIDATION_CONTEXT": "NEUTRAL",
        "REGIME": "TREND_CONTINUATION",
        "SWING_BIAS": "BULLISH",
    }
    external = {
        "oi_current": None,
        "oi_delta_5m": None,
        "oi_delta_15m": None,
        "oi_delta_1h": None,
        "oi_delta_4h": None,
        "funding_current": None,
        "funding_percentile": None,
        "funding_change": None,
        "liquidation_5m": {"long": None, "short": None, "total": None, "imbalance": None, "coverage_status": "UNAVAILABLE"},
        "liquidation_15m": {"long": None, "short": None, "total": None, "imbalance": None, "coverage_status": "UNAVAILABLE"},
        "liquidation_1h": {"long": None, "short": None, "total": None, "imbalance": None, "coverage_status": "UNAVAILABLE"},
        "source_evidence_ids": [],
        "provenance": "historical external raw fields were not persisted; left null to avoid hindsight",
    }
    return build_origin_candidate(
        symbol=symbol,
        direction="LONG",
        observation_number=observation_number,
        snapshot_timestamp=timestamp,
        price=price,
        production_sequence_id=sequence_id,
        production_sequence_state="INVALIDATED",
        production_review_state="NO_TRADE",
        opportunity_evidence=evidence,
        external_evidence=external,
    )


def _snapshot_from_candidate(
    candidate: dict[str, Any],
    origin_price: float,
    trajectory_state: str,
    previous_price: float | None = None,
) -> dict[str, Any]:
    if trajectory_state not in TRAJECTORY_STATES:
        raise ValueError(f"invalid trajectory state: {trajectory_state}")
    price = float(candidate["price"])
    return {
        "observation_number": candidate["observation_number"],
        "snapshot_timestamp": candidate["snapshot_timestamp"],
        "available_at": candidate["available_at"],
        "price": price,
        "production_sequence_id": candidate["production_sequence_id"],
        "production_sequence_state": candidate["production_sequence_state"],
        "production_review_state": candidate["production_review_state"],
        "opportunity_evidence": deepcopy(candidate["opportunity_evidence"]),
        "external_evidence": deepcopy(candidate["external_evidence"]),
        "price_change_from_origin_pct": _pct_change(candidate["direction"], origin_price, price),
        "price_change_from_previous_snapshot_pct": None if previous_price is None else _pct_change(candidate["direction"], previous_price, price),
        "trajectory_state": trajectory_state,
        "relation_features": relation_features(candidate, previous_price=previous_price),
    }


def relation_features(candidate: dict[str, Any], previous_price: float | None = None) -> list[str]:
    external = candidate.get("external_evidence") or {}
    labels: list[str] = []
    if _has_complete_zero_liquidation_context(external):
        labels.append("ZERO_LIQUIDATION_CONTEXT")
    oi_1h = external.get("oi_delta_1h")
    oi_4h = external.get("oi_delta_4h")
    current_price = _number(candidate.get("price"))
    if isinstance(oi_1h, (int, float)):
        relation = _directional_price_oi_label(previous_price, current_price, oi_1h)
        if relation:
            labels.append(relation)
    if isinstance(oi_4h, (int, float)) and oi_4h > 0:
        labels.append("POSITIONING_REBUILD")
    return [item for item in labels if item in RESEARCH_RELATION_LABELS]


def pending_outcome_required_start_by_symbol(store: dict[str, Any], timeframe: str = "M5") -> dict[str, int]:
    required: dict[str, int] = {}
    seconds = TIMEFRAME_SECONDS[timeframe]
    for record in store.get("records", []):
        outcomes = record.get("outcomes") or {}
        if outcomes and all(outcome.get("horizon_status") == "COMPLETE" for outcome in outcomes.values()):
            continue
        if not any((outcome.get("horizon_status") in {"PENDING", "DATA_GAP"}) for outcome in outcomes.values()):
            continue
        origin = _optional_int(record.get("origin_snapshot_timestamp"))
        symbol = record.get("symbol")
        if origin is None or not symbol:
            continue
        first_required = first_required_m5_after_origin(origin, seconds)
        current = required.get(str(symbol))
        required[str(symbol)] = first_required if current is None else min(current, first_required)
    return required


def first_required_m5_after_origin(origin_timestamp: int, seconds: int = 300) -> int:
    timestamp = int(origin_timestamp)
    return timestamp + (seconds - timestamp % seconds if timestamp % seconds else seconds)


def _trajectory_state(direction: str, origin_price: float, previous_price: float, current_price: float) -> str:
    previous = _pct_change(direction, origin_price, previous_price)
    current = _pct_change(direction, origin_price, current_price)
    if current > previous:
        return "IMPROVING"
    if current < previous:
        return "DETERIORATING"
    return "STABLE"


def _pct_change(direction: str, base_price: float, current_price: float) -> float:
    if direction == "LONG":
        return (current_price / base_price - 1) * 100
    if direction == "SHORT":
        return (base_price / current_price - 1) * 100
    raise ValueError(f"unsupported direction: {direction}")


def _empty_outcomes(origin_timestamp: int) -> dict[str, Any]:
    return {
        horizon: {
            "horizon_status": "PENDING",
            "reference_timestamp": int(origin_timestamp),
            "window_end_timestamp": int(origin_timestamp) + seconds,
            "MFE_pct": None,
            "MAE_pct": None,
            "MFE_price": None,
            "MAE_price": None,
            "time_to_MFE": None,
            "time_to_MAE": None,
            "price_field": "closed candle high/low",
        }
        for horizon, seconds in HORIZON_SECONDS.items()
    }


def _historical_external(external: dict[str, Any] | None) -> dict[str, Any]:
    template = {
        "oi_current": None,
        "oi_delta_5m": None,
        "oi_delta_15m": None,
        "oi_delta_1h": None,
        "oi_delta_4h": None,
        "funding_current": None,
        "funding_percentile": None,
        "funding_change": None,
        "liquidation_5m": None,
        "liquidation_15m": None,
        "liquidation_1h": None,
        "source_evidence_ids": [],
        "provenance": "provided contemporaneous research evidence",
    }
    if external:
        template.update(deepcopy(external))
    return template


def _canonical_context(context: dict[str, Any]) -> dict[str, Any]:
    return {str(key): context[key] for key in sorted(context)}


def _with_episode_defaults(store: dict[str, Any]) -> dict[str, Any]:
    for record in store.get("records", []):
        status = record.get("status")
        if record.get("episode_status") not in EPISODE_STATUSES:
            record["episode_status"] = "CLOSED" if status in {"CONVERTED", "TERMINAL", "OUTCOME_COMPLETE"} else "OPEN"
        record.setdefault("context_break_observation", None)
        record.setdefault("context_break_timestamp", None)
        record.setdefault("context_break_reason", None)
        record.setdefault("context_break_reasons", [])
    return store


def _episode_status(record: dict[str, Any]) -> str:
    status = record.get("episode_status")
    if status in EPISODE_STATUSES:
        return str(status)
    return "CLOSED" if record.get("status") in {"CONVERTED", "TERMINAL", "OUTCOME_COMPLETE"} else "OPEN"


def _validate_store_is_research_only(store: dict[str, Any]) -> None:
    if store.get("schema") != TRACKER_SCHEMA:
        raise ValueError("invalid tracker schema")
    payload = json.dumps(store, sort_keys=True).lower()
    for key in FORBIDDEN_EXECUTION_KEYS:
        if f'"{key}"' in payload:
            raise ValueError(f"execution field forbidden in research tracker: {key}")


def _directional_price_oi_label(previous_price: float | None, current_price: float | None, oi_delta: float) -> str | None:
    if previous_price is None or current_price is None:
        return None
    price_delta = current_price - float(previous_price)
    if price_delta > 0 and oi_delta > 0:
        return "PRICE_UP_OI_UP"
    if price_delta > 0 and oi_delta < 0:
        return "PRICE_UP_OI_DOWN"
    if price_delta < 0 and oi_delta > 0:
        return "PRICE_DOWN_OI_UP"
    if price_delta < 0 and oi_delta < 0:
        return "PRICE_DOWN_OI_DOWN"
    if price_delta == 0 and oi_delta > 0:
        return "PRICE_FLAT_OI_UP"
    if price_delta == 0 and oi_delta < 0:
        return "PRICE_FLAT_OI_DOWN"
    return "PRICE_OI_NEUTRAL"


def _has_complete_zero_liquidation_context(external: dict[str, Any]) -> bool:
    for key in ("liquidation_5m", "liquidation_15m", "liquidation_1h"):
        window = external.get(key)
        if not isinstance(window, dict):
            continue
        if window.get("coverage_status") not in {"AVAILABLE", "COMPLETE"}:
            continue
        if window.get("long") == 0 and window.get("short") == 0:
            return True
    return False


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
