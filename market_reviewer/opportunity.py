"""V4.2 opportunity feature and outcome data layer.

This module is intentionally read-only with respect to production review
state.  It extracts deterministic measurements from closed-candle market data
and an already-computed production review.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any

from .model import MarketDataFrame


FEATURE_SCHEMA_VERSION = "opportunity-features.v1"
OUTCOME_SCHEMA_VERSION = "outcome-tracking.v1"
FEATURE_REGISTRY_VERSION = "feature-registry.v1"
CORE_FEATURE_LIMIT = 20
DECISION_FEATURE_LIMIT = 20

FEATURE_DOMAINS = {
    "STRUCTURE",
    "LIQUIDITY",
    "MOMENTUM",
    "LOCATION",
    "FRESHNESS",
    "OPPORTUNITY",
    "CONTEXT",
}
FEATURE_TYPES = {"DECISION_FEATURE", "OBSERVATION_FEATURE", "OUTCOME_FEATURE"}
FEATURE_STATUSES = {"EXPERIMENTAL", "CANDIDATE", "CORE", "DEPRECATED"}
FAILURE_SIGNATURES = {
    "SWEEP_WITHOUT_RECLAIM",
    "WEAK_DISPLACEMENT",
    "MSS_TOO_LATE",
    "HIGH_EXTENSION",
    "POOR_REMAINING_ROOM",
    "POI_OVER_MITIGATED",
    "POI_EXHAUSTED",
    "RANGE_MIDPOINT",
    "DEAD_SESSION",
    "HTF_CONFLICT",
    "SETUP_WITHOUT_RETEST",
    "OPPORTUNITY_TOO_EXTENDED",
    "EXPIRED_NO_TRIGGER",
}
OUTCOME_ORIGINS = {
    "GATE_A",
    "GATE_B",
    "CANDIDATE_POI",
    "ELIGIBLE_RETEST",
    "PRODUCTION_ARMED",
    "SEQUENCE_GENESIS",
}
FORWARD_WINDOWS = ("1H", "4H", "12H", "24H", "SEQUENCE_TERMINAL")


@dataclass(frozen=True)
class FeatureDefinition:
    feature_id: str
    name: str
    domain: str
    feature_type: str
    status: str
    version: int
    description: str
    formula: str
    available_at_rule: str


FEATURE_REGISTRY: tuple[FeatureDefinition, ...] = (
    FeatureDefinition("HTF_ALIGNMENT", "HTF Alignment", "STRUCTURE", "DECISION_FEATURE", "CANDIDATE", 1, "D1/H4 agreement with swing bias.", "Count D1/H4 structure states matching Swing_Bias.", "latest closed review timestamp"),
    FeatureDefinition("CONTEXTUAL_MSS_QUALITY", "Contextual MSS Quality", "STRUCTURE", "DECISION_FEATURE", "EXPERIMENTAL", 1, "Presence and recency of contextual MSS/BOS evidence.", "Contextual_MSS timestamp and bars_since_contextual_mss.", "contextual MSS evidence timestamp"),
    FeatureDefinition("LIQUIDITY_FRESHNESS", "Liquidity Freshness", "LIQUIDITY", "DECISION_FEATURE", "CANDIDATE", 1, "Age and interaction count of the active liquidity reference.", "liquidity_age_bars plus liquidity_touch_count.", "active tactical draw selected_at"),
    FeatureDefinition("SWEEP_RECLAIM_QUALITY", "Sweep Reclaim Quality", "LIQUIDITY", "DECISION_FEATURE", "EXPERIMENTAL", 1, "Depth and reclaim behavior after active sweep.", "sweep_depth_pct/atr, reclaim_bars, reclaim_close_position.", "latest active reclaim timestamp"),
    FeatureDefinition("DISPLACEMENT_STRENGTH", "Displacement Strength", "MOMENTUM", "DECISION_FEATURE", "CANDIDATE", 1, "Body/range expansion characteristics of valid displacement.", "displacement_body_pct/atr, displacement_range_atr, close position.", "displacement confirmation timestamp"),
    FeatureDefinition("BOS_QUALITY", "BOS Quality", "MOMENTUM", "DECISION_FEATURE", "EXPERIMENTAL", 1, "Distance and quality of structure break.", "bos_distance_atr and structural_break_tf.", "BOS/MSS evidence timestamp"),
    FeatureDefinition("HTF_LOCATION_QUALITY", "HTF Location Quality", "LOCATION", "DECISION_FEATURE", "EXPERIMENTAL", 1, "Location against HTF dealing range and liquidity.", "premium_discount_position plus nearest HTF liquidity distance.", "latest closed review timestamp"),
    FeatureDefinition("RANGE_EDGE_LOCATION", "Range Edge Location", "LOCATION", "DECISION_FEATURE", "EXPERIMENTAL", 1, "Distance to the nearest HTF range edge.", "range_edge_distance_pct.", "latest closed review timestamp"),
    FeatureDefinition("POI_FRESHNESS", "POI Freshness", "FRESHNESS", "DECISION_FEATURE", "CANDIDATE", 1, "Age and freshness of current setup POI.", "poi_age_bars and bars_since_setup_creation.", "setup POI formed_at"),
    FeatureDefinition("MITIGATION_STATE", "Mitigation State", "FRESHNESS", "DECISION_FEATURE", "CANDIDATE", 1, "Mitigation status of the setup POI.", "mitigation_count, filled_ratio, remaining_unfilled_ratio.", "latest closed review timestamp"),
    FeatureDefinition("EXECUTION_EXTENSION", "Execution Extension", "OPPORTUNITY", "DECISION_FEATURE", "EXPERIMENTAL", 1, "How far price has extended from genesis/MSS.", "extension_from_genesis_atr and extension_from_mss_atr.", "latest closed review timestamp"),
    FeatureDefinition("REMAINING_ROOM", "Remaining Room", "OPPORTUNITY", "DECISION_FEATURE", "EXPERIMENTAL", 1, "Distance to next relevant draw or invalidation when objectively available.", "remaining_room_pct and remaining_room_r_if_defined.", "latest closed review timestamp"),
    FeatureDefinition("REGIME", "Regime", "CONTEXT", "OBSERVATION_FEATURE", "CANDIDATE", 1, "Production market regime label.", "Market_Regime copied from production truth.", "latest closed review timestamp"),
    FeatureDefinition("TREND_MATURITY", "Trend Maturity", "CONTEXT", "OBSERVATION_FEATURE", "EXPERIMENTAL", 1, "Coarse count of aligned continuation structure.", "Aligned HTF count and phase.", "latest closed review timestamp"),
    FeatureDefinition("VOLATILITY_STATE", "Volatility State", "CONTEXT", "OBSERVATION_FEATURE", "EXPERIMENTAL", 1, "Current short/long volatility relationship.", "short_long_volatility_ratio with percentile unavailable unless history exists.", "latest closed review timestamp"),
    FeatureDefinition("SESSION", "Session", "CONTEXT", "OBSERVATION_FEATURE", "EXPERIMENTAL", 1, "UTC session bucket at snapshot time.", "Hour-of-day session classification.", "snapshot timestamp"),
)

RAW_METRIC_IDS = {
    "STRUCTURE": ("htf_alignment_count", "structural_break_tf", "bars_since_contextual_mss"),
    "LIQUIDITY": ("liquidity_age_bars", "liquidity_touch_count", "liquidity_distance_pct", "sweep_depth_pct", "sweep_depth_atr", "reclaim_bars", "reclaim_close_position"),
    "MOMENTUM": ("displacement_body_pct", "displacement_body_atr", "displacement_range_atr", "displacement_close_position", "bos_distance_atr", "volume_ratio_if_available"),
    "LOCATION": ("premium_discount_position", "range_edge_distance_pct", "nearest_htf_liquidity_distance", "poi_to_external_liquidity_distance"),
    "FRESHNESS": ("poi_age_bars", "mitigation_count", "filled_ratio", "remaining_unfilled_ratio", "bars_since_setup_creation"),
    "OPPORTUNITY": ("extension_from_genesis_atr", "extension_from_mss_atr", "distance_to_active_pct", "distance_to_poi_pct", "remaining_room_pct", "remaining_room_r_if_defined"),
    "CONTEXT": ("session", "regime", "trend_maturity", "volatility_percentile", "short_long_volatility_ratio"),
}


def validate_feature_registry(registry: tuple[FeatureDefinition, ...] = FEATURE_REGISTRY) -> None:
    core_count = sum(1 for item in registry if item.status == "CORE")
    decision_candidates = [
        item for item in registry
        if item.feature_type == "DECISION_FEATURE" and item.status != "DEPRECATED"
    ]
    if core_count > CORE_FEATURE_LIMIT:
        raise ValueError("feature registry core cap exceeded")
    if len(decision_candidates) > DECISION_FEATURE_LIMIT:
        raise ValueError("decision feature cap exceeded")
    conceptual_keys: set[tuple[str, str]] = set()
    for item in registry:
        if item.domain not in FEATURE_DOMAINS:
            raise ValueError(f"invalid feature domain: {item.domain}")
        if item.feature_type not in FEATURE_TYPES:
            raise ValueError(f"invalid feature type: {item.feature_type}")
        if item.status not in FEATURE_STATUSES:
            raise ValueError(f"invalid feature status: {item.status}")
        key = (item.domain, item.name.lower())
        if item.feature_type == "DECISION_FEATURE" and item.status != "DEPRECATED":
            if key in conceptual_keys:
                raise ValueError(f"duplicate conceptual decision feature: {item.name}")
            conceptual_keys.add(key)


def feature_registry_document() -> dict[str, Any]:
    validate_feature_registry()
    return {
        "schema_version": FEATURE_REGISTRY_VERSION,
        "core_feature_limit": CORE_FEATURE_LIMIT,
        "decision_feature_limit": DECISION_FEATURE_LIMIT,
        "features": [asdict(item) for item in FEATURE_REGISTRY],
    }


def decision_candidate_features(registry: tuple[FeatureDefinition, ...] = FEATURE_REGISTRY) -> list[FeatureDefinition]:
    validate_feature_registry(registry)
    return [
        item for item in registry
        if item.feature_type == "DECISION_FEATURE" and item.status != "DEPRECATED"
    ]


def extract_opportunity_snapshot(review: dict[str, Any], frames: dict[str, MarketDataFrame]) -> dict[str, Any]:
    validate_feature_registry()
    snapshot_timestamp = _int_or_none(review.get("Review_Timestamp")) or _latest_closed_timestamp(frames)
    generation_id = frames["D1"].generation_id
    symbol = str(review.get("Symbol") or frames["D1"].symbol)
    sequence_id = str(review.get("Sequence_ID") or "NONE")
    active = _parse_level_text(str(review.get("Active_Tactical_Draw") or "NONE"))
    setup = _parse_setup_text(str(review.get("Setup_FVG") or "NONE"))
    raw_metrics, availability = _raw_metrics(review, frames, active, setup, snapshot_timestamp)
    features = _features(review, raw_metrics, availability, active, setup, snapshot_timestamp)
    failure_signatures = _failure_signatures(review, raw_metrics)
    for item in features.values():
        available_at = item.get("available_at")
        if isinstance(available_at, int) and available_at > snapshot_timestamp:
            raise ValueError(f"feature available_at after snapshot: {item.get('feature_id')}")
    return {
        "schema_version": FEATURE_SCHEMA_VERSION,
        "opportunity_id": f"{FEATURE_SCHEMA_VERSION}:{symbol}:{sequence_id}:{snapshot_timestamp}:{generation_id}",
        "symbol": symbol,
        "sequence_id": sequence_id,
        "sequence_state": review.get("Sequence_State"),
        "snapshot_timestamp": snapshot_timestamp,
        "market_data_generation_id": generation_id,
        "opportunity_status": "ACTIVE_OPPORTUNITY" if active else "NO_ACTIVE_OPPORTUNITY",
        "truth": _truth(review, active, setup),
        "raw_metrics": raw_metrics,
        "features": features,
        "failure_signatures": failure_signatures,
        "availability": availability,
    }


def build_outcome_tracking(
    opportunity_snapshot: dict[str, Any],
    measurement_origin: str,
    origin_timestamp: int,
    origin_price: float,
    forward_windows: dict[str, dict[str, Any]] | None = None,
    r_definition: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if measurement_origin not in OUTCOME_ORIGINS:
        raise ValueError(f"invalid outcome origin: {measurement_origin}")
    windows = forward_windows or {
        window: {
            "MFE": None,
            "MAE": None,
            "time_to_MFE": None,
            "time_to_MAE": None,
            "time_to_invalidation": None,
            "reached_1R": None,
            "reached_2R": None,
            "reached_3R": None,
            "expired_without_trigger": None,
            "no_retest": None,
            "invalidated": None,
        }
        for window in FORWARD_WINDOWS
    }
    return {
        "schema_version": OUTCOME_SCHEMA_VERSION,
        "opportunity_id": opportunity_snapshot["opportunity_id"],
        "symbol": opportunity_snapshot["symbol"],
        "sequence_id": opportunity_snapshot["sequence_id"],
        "measurement_origin": measurement_origin,
        "origin_timestamp": origin_timestamp,
        "origin_price": origin_price,
        "r_definition": r_definition,
        "forward_windows": windows,
    }


def _raw_metrics(
    review: dict[str, Any],
    frames: dict[str, MarketDataFrame],
    active: dict[str, Any] | None,
    setup: dict[str, Any] | None,
    snapshot_timestamp: int,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    availability: dict[str, str] = {}
    raw = {domain: {metric: None for metric in metrics} for domain, metrics in RAW_METRIC_IDS.items()}
    price = frames["M5"].closed_candles()[-1].close
    htf_states = review.get("Structure_State", {})
    bias = review.get("Swing_Bias")
    raw["STRUCTURE"]["htf_alignment_count"] = sum(1 for tf in ("D1", "H4") if htf_states.get(tf) == bias)
    raw["STRUCTURE"]["structural_break_tf"] = _structural_break_tf(review)
    mss_timestamp = _timestamp_from_text(str(review.get("Contextual_MSS") or "NONE"))
    raw["STRUCTURE"]["bars_since_contextual_mss"] = _bars_since(frames["M5"], mss_timestamp) if mss_timestamp else _unavailable(availability, "STRUCTURE.bars_since_contextual_mss", "contextual MSS unavailable")

    if active:
        frame = frames.get(active["timeframe"])
        raw["LIQUIDITY"]["liquidity_age_bars"] = _bars_since(frame, active.get("formed_at")) if frame else _unavailable(availability, "LIQUIDITY.liquidity_age_bars", "active timeframe unavailable")
        raw["LIQUIDITY"]["liquidity_touch_count"] = _active_touch_count(review, active)
        raw["LIQUIDITY"]["liquidity_distance_pct"] = abs(price - active["price"]) / price * 100
        raw["OPPORTUNITY"]["distance_to_active_pct"] = raw["LIQUIDITY"]["liquidity_distance_pct"]
    else:
        for key in ("liquidity_age_bars", "liquidity_touch_count", "liquidity_distance_pct"):
            _unavailable(availability, f"LIQUIDITY.{key}", "active tactical draw unavailable")
        _unavailable(availability, "OPPORTUNITY.distance_to_active_pct", "active tactical draw unavailable")

    sweep = _event_for_active(review, active, "SWEPT")
    reclaim = _event_for_active(review, active, "RECLAIMED")
    if sweep:
        raw["LIQUIDITY"]["sweep_depth_pct"] = float(sweep.get("penetration") or 0) / active["price"] * 100 if active else None
        raw["LIQUIDITY"]["sweep_depth_atr"] = _atr_ratio(frames.get(str(sweep.get("timeframe"))), float(sweep.get("penetration") or 0))
    else:
        _unavailable(availability, "LIQUIDITY.sweep_depth_pct", "active sweep unavailable")
        _unavailable(availability, "LIQUIDITY.sweep_depth_atr", "active sweep unavailable")
    if sweep and reclaim:
        seconds = _timeframe_seconds(str(reclaim.get("timeframe") or "M5"))
        raw["LIQUIDITY"]["reclaim_bars"] = max(0, int((int(reclaim["timestamp"]) - int(sweep["timestamp"])) / seconds))
        raw["LIQUIDITY"]["reclaim_close_position"] = reclaim.get("close_location")
    else:
        _unavailable(availability, "LIQUIDITY.reclaim_bars", "active sweep/reclaim sequence unavailable")
        _unavailable(availability, "LIQUIDITY.reclaim_close_position", "active reclaim unavailable")

    displacement_timestamp = _timestamp_from_text(str(review.get("Displacement") or "NONE"))
    body_ratio = _number_from_text(str(review.get("Displacement") or ""), "body_ratio")
    range_ratio = _number_from_text(str(review.get("Displacement") or ""), "range_ratio")
    if displacement_timestamp:
        raw["MOMENTUM"]["displacement_body_pct"] = body_ratio
        raw["MOMENTUM"]["displacement_body_atr"] = body_ratio
        raw["MOMENTUM"]["displacement_range_atr"] = range_ratio
        raw["MOMENTUM"]["displacement_close_position"] = "NEAR_EXTREME" if "near_extreme=True" in str(review.get("Displacement")) else None
    else:
        for key in ("displacement_body_pct", "displacement_body_atr", "displacement_range_atr", "displacement_close_position"):
            _unavailable(availability, f"MOMENTUM.{key}", "valid displacement unavailable")
    raw["MOMENTUM"]["bos_distance_atr"] = _unavailable(availability, "MOMENTUM.bos_distance_atr", "BOS distance source unavailable")
    raw["MOMENTUM"]["volume_ratio_if_available"] = _unavailable(availability, "MOMENTUM.volume_ratio_if_available", "volume baseline unavailable")

    raw["LOCATION"]["premium_discount_position"] = review.get("Premium_Discount")
    raw["LOCATION"]["range_edge_distance_pct"] = _range_edge_distance(frames["H4"], price)
    raw["LOCATION"]["nearest_htf_liquidity_distance"] = _nearest_htf_liquidity_distance(review, price)
    raw["LOCATION"]["poi_to_external_liquidity_distance"] = _unavailable(availability, "LOCATION.poi_to_external_liquidity_distance", "external target mapping unavailable")

    if setup:
        raw["FRESHNESS"]["poi_age_bars"] = _bars_since(frames.get(setup["timeframe"]), setup["formed_at"])
        raw["FRESHNESS"]["mitigation_count"] = int(setup.get("touch_count") or 0)
        raw["FRESHNESS"]["filled_ratio"] = 0.0 if setup.get("status") == "FRESH" else None
        raw["FRESHNESS"]["remaining_unfilled_ratio"] = 1.0 if setup.get("status") == "FRESH" else None
        raw["FRESHNESS"]["bars_since_setup_creation"] = raw["FRESHNESS"]["poi_age_bars"]
        raw["OPPORTUNITY"]["distance_to_poi_pct"] = abs(price - setup["midpoint"]) / price * 100
    else:
        for key in ("poi_age_bars", "mitigation_count", "filled_ratio", "remaining_unfilled_ratio", "bars_since_setup_creation"):
            _unavailable(availability, f"FRESHNESS.{key}", "setup POI unavailable")
        _unavailable(availability, "OPPORTUNITY.distance_to_poi_pct", "setup POI unavailable")

    genesis = _int_or_none(review.get("Sequence_Started_At"))
    if genesis:
        raw["OPPORTUNITY"]["extension_from_genesis_atr"] = _extension_atr(frames["M5"], genesis)
    else:
        _unavailable(availability, "OPPORTUNITY.extension_from_genesis_atr", "sequence genesis unavailable")
    raw["OPPORTUNITY"]["extension_from_mss_atr"] = _extension_atr(frames["M5"], mss_timestamp) if mss_timestamp else _unavailable(availability, "OPPORTUNITY.extension_from_mss_atr", "contextual MSS unavailable")
    raw["OPPORTUNITY"]["remaining_room_pct"] = _unavailable(availability, "OPPORTUNITY.remaining_room_pct", "objective target unavailable")
    raw["OPPORTUNITY"]["remaining_room_r_if_defined"] = _unavailable(availability, "OPPORTUNITY.remaining_room_r_if_defined", "objective R unavailable")

    raw["CONTEXT"]["session"] = _session(snapshot_timestamp)
    raw["CONTEXT"]["regime"] = review.get("Market_Regime")
    raw["CONTEXT"]["trend_maturity"] = _trend_maturity(raw["STRUCTURE"]["htf_alignment_count"], review.get("Current_Phase"))
    raw["CONTEXT"]["volatility_percentile"] = _unavailable(availability, "CONTEXT.volatility_percentile", "historical volatility distribution unavailable")
    raw["CONTEXT"]["short_long_volatility_ratio"] = _short_long_volatility_ratio(frames["M5"])
    return raw, availability


def _features(
    review: dict[str, Any],
    raw: dict[str, dict[str, Any]],
    availability: dict[str, str],
    active: dict[str, Any] | None,
    setup: dict[str, Any] | None,
    snapshot_timestamp: int,
) -> dict[str, dict[str, Any]]:
    feature_map = {item.feature_id: item for item in FEATURE_REGISTRY}

    def pack(feature_id: str, value: Any, available_at: int | None = None, evidence: list[str] | None = None) -> dict[str, Any]:
        definition = feature_map[feature_id]
        return {
            "feature_id": feature_id,
            "feature_type": definition.feature_type,
            "domain": definition.domain,
            "status": definition.status,
            "version": definition.version,
            "available_at": min(available_at or snapshot_timestamp, snapshot_timestamp),
            "source_evidence_ids": evidence or [],
            "value": value,
        }

    liquidity_age = raw["LIQUIDITY"]["liquidity_age_bars"]
    liquidity_touches = raw["LIQUIDITY"]["liquidity_touch_count"]
    poi_age = raw["FRESHNESS"]["poi_age_bars"]
    mitigation_count = raw["FRESHNESS"]["mitigation_count"]
    extension = raw["OPPORTUNITY"]["extension_from_genesis_atr"]
    return {
        "HTF_ALIGNMENT": pack("HTF_ALIGNMENT", {"aligned_count": raw["STRUCTURE"]["htf_alignment_count"], "bias": review.get("Swing_Bias")}),
        "CONTEXTUAL_MSS_QUALITY": pack("CONTEXTUAL_MSS_QUALITY", {"bars_since_contextual_mss": raw["STRUCTURE"]["bars_since_contextual_mss"], "status": "AVAILABLE" if raw["STRUCTURE"]["bars_since_contextual_mss"] is not None else "MISSING"}),
        "LIQUIDITY_FRESHNESS": pack("LIQUIDITY_FRESHNESS", {"age_bars": liquidity_age, "touch_count": liquidity_touches, "status": _freshness_status(liquidity_age, liquidity_touches)}, active.get("selected_at") if active else None),
        "SWEEP_RECLAIM_QUALITY": pack("SWEEP_RECLAIM_QUALITY", {"sweep_depth_pct": raw["LIQUIDITY"]["sweep_depth_pct"], "reclaim_bars": raw["LIQUIDITY"]["reclaim_bars"], "status": "AVAILABLE" if raw["LIQUIDITY"]["reclaim_bars"] is not None else "MISSING"}),
        "DISPLACEMENT_STRENGTH": pack("DISPLACEMENT_STRENGTH", {"body_atr": raw["MOMENTUM"]["displacement_body_atr"], "range_atr": raw["MOMENTUM"]["displacement_range_atr"], "status": "AVAILABLE" if raw["MOMENTUM"]["displacement_range_atr"] is not None else "MISSING"}),
        "BOS_QUALITY": pack("BOS_QUALITY", {"bos_distance_atr": raw["MOMENTUM"]["bos_distance_atr"], "structural_break_tf": raw["STRUCTURE"]["structural_break_tf"]}),
        "HTF_LOCATION_QUALITY": pack("HTF_LOCATION_QUALITY", {"premium_discount_position": raw["LOCATION"]["premium_discount_position"], "nearest_htf_liquidity_distance": raw["LOCATION"]["nearest_htf_liquidity_distance"]}),
        "RANGE_EDGE_LOCATION": pack("RANGE_EDGE_LOCATION", {"range_edge_distance_pct": raw["LOCATION"]["range_edge_distance_pct"]}),
        "POI_FRESHNESS": pack("POI_FRESHNESS", {"age_bars": poi_age, "status": _poi_freshness_status(poi_age, setup)}, setup.get("formed_at") if setup else None),
        "MITIGATION_STATE": pack("MITIGATION_STATE", {"mitigation_count": mitigation_count, "filled_ratio": raw["FRESHNESS"]["filled_ratio"], "remaining_unfilled_ratio": raw["FRESHNESS"]["remaining_unfilled_ratio"]}, setup.get("formed_at") if setup else None),
        "EXECUTION_EXTENSION": pack("EXECUTION_EXTENSION", {"extension_from_genesis_atr": extension, "extension_from_mss_atr": raw["OPPORTUNITY"]["extension_from_mss_atr"], "status": "HIGH_EXTENSION" if isinstance(extension, float) and extension > 3 else "NORMAL_OR_UNAVAILABLE"}),
        "REMAINING_ROOM": pack("REMAINING_ROOM", {"remaining_room_pct": raw["OPPORTUNITY"]["remaining_room_pct"], "remaining_room_r_if_defined": raw["OPPORTUNITY"]["remaining_room_r_if_defined"]}),
        "REGIME": pack("REGIME", {"regime": raw["CONTEXT"]["regime"]}),
        "TREND_MATURITY": pack("TREND_MATURITY", {"trend_maturity": raw["CONTEXT"]["trend_maturity"]}),
        "VOLATILITY_STATE": pack("VOLATILITY_STATE", {"volatility_percentile": raw["CONTEXT"]["volatility_percentile"], "short_long_volatility_ratio": raw["CONTEXT"]["short_long_volatility_ratio"]}),
        "SESSION": pack("SESSION", {"session": raw["CONTEXT"]["session"]}),
    }


def _truth(review: dict[str, Any], active: dict[str, Any] | None, setup: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "swing_bias": review.get("Swing_Bias"),
        "current_phase": review.get("Current_Phase"),
        "market_regime": review.get("Market_Regime"),
        "sequence_state": review.get("Sequence_State"),
        "state": review.get("State"),
        "active_tactical_draw": active,
        "active_sweep": any(item.get("event_type") == "SWEPT" for item in review.get("Liquidity_Events", []) if _same_level(item, active)),
        "setup_poi": setup,
        "eligible_retest": review.get("Eligible_Retest_Confirmed") == "YES",
    }


def _failure_signatures(review: dict[str, Any], raw: dict[str, dict[str, Any]]) -> list[str]:
    signatures: list[str] = []
    if review.get("Sequence_State") == "EXPIRED_NO_TRIGGER":
        signatures.append("EXPIRED_NO_TRIGGER")
    if review.get("Sequence_State") == "RETEST_PENDING" and review.get("Eligible_Retest_Confirmed") != "YES":
        signatures.append("SETUP_WITHOUT_RETEST")
    extension = raw["OPPORTUNITY"]["extension_from_genesis_atr"]
    if isinstance(extension, float) and extension > 3:
        signatures.append("OPPORTUNITY_TOO_EXTENDED")
        signatures.append("HIGH_EXTENSION")
    room = raw["OPPORTUNITY"]["remaining_room_pct"]
    if isinstance(room, float) and room < 0.5:
        signatures.append("POOR_REMAINING_ROOM")
    return [item for item in signatures if item in FAILURE_SIGNATURES]


def _parse_level_text(text: str) -> dict[str, Any] | None:
    if not text or text == "NONE":
        return None
    match = re.search(r"^(.*?) ([0-9]+(?:\.[0-9]+)?) on (\w+), formed_at=([0-9]+)(?:, distance=([0-9.]+))?", text)
    if not match:
        return None
    level_type, price, timeframe, formed_at, distance = match.groups()
    return {
        "type": level_type,
        "price": float(price),
        "timeframe": timeframe,
        "formed_at": int(formed_at),
        "distance": None if distance is None else float(distance),
        "liquidity_id": f"{timeframe}-{_liquidity_side(level_type)}-{formed_at}",
    }


def _parse_setup_text(text: str) -> dict[str, Any] | None:
    if not text or text == "NONE":
        return None
    match = re.search(r"^(BULLISH|BEARISH) SETUP_FVG (\w+) ([0-9]+(?:\.[0-9]+)?)-([0-9]+(?:\.[0-9]+)?) @ ([0-9]+); status=([A-Z_]+)", text)
    if not match:
        return None
    direction, timeframe, lower, upper, formed_at, status = match.groups()
    lower_value = float(lower)
    upper_value = float(upper)
    return {
        "type": "SETUP_FVG",
        "direction": direction,
        "timeframe": timeframe,
        "lower": lower_value,
        "upper": upper_value,
        "midpoint": (lower_value + upper_value) / 2,
        "formed_at": int(formed_at),
        "status": status,
        "touch_count": 0,
    }


def _liquidity_side(level_type: str) -> str:
    if "Equal Lows" in level_type:
        return "EQL"
    if "Equal Highs" in level_type:
        return "EQH"
    if "Sell-side" in level_type:
        return "SSL"
    if "Buy-side" in level_type:
        return "BSL"
    return level_type.upper().replace(" ", "_")


def _latest_closed_timestamp(frames: dict[str, MarketDataFrame]) -> int:
    return max(frame.latest_closed_candle_timestamp for frame in frames.values())


def _bars_since(frame: MarketDataFrame | None, timestamp: int | None) -> int | None:
    if frame is None or timestamp is None:
        return None
    return len([candle for candle in frame.closed_candles() if candle.timestamp > timestamp])


def _active_touch_count(review: dict[str, Any], active: dict[str, Any]) -> int:
    return sum(1 for item in review.get("Liquidity_Events", []) if _same_level(item, active) and item.get("event_type") in {"APPROACHED", "SWEPT", "RECLAIMED"})


def _event_for_active(review: dict[str, Any], active: dict[str, Any] | None, event_type: str) -> dict[str, Any] | None:
    if not active:
        return None
    events = [
        item for item in review.get("Liquidity_Events", [])
        if _same_level(item, active) and item.get("event_type") == event_type
    ]
    return max(events, key=lambda item: int(item.get("timestamp", 0)), default=None)


def _same_level(event: dict[str, Any], active: dict[str, Any] | None) -> bool:
    if not active:
        return False
    return (
        abs(float(event.get("level_price", -1)) - active["price"]) < 0.01
        and event.get("timeframe") == active["timeframe"]
        and event.get("level_type") == active["type"]
    )


def _timestamp_from_text(text: str) -> int | None:
    match = re.search(r"@ ([0-9]+)", text)
    return int(match.group(1)) if match else None


def _number_from_text(text: str, name: str) -> float | None:
    match = re.search(rf"{name}=([0-9.]+)", text)
    return float(match.group(1)) if match else None


def _structural_break_tf(review: dict[str, Any]) -> str | None:
    if review.get("Contextual_MSS") and review.get("Contextual_MSS") != "NONE":
        return "CONTEXTUAL"
    for timeframe, value in (review.get("Last_BOS") or {}).items():
        if value and value != "NONE":
            return str(timeframe)
    return None


def _timeframe_seconds(timeframe: str) -> int:
    return {"D1": 86400, "H4": 14400, "H1": 3600, "M15": 900, "M5": 300}.get(timeframe, 300)


def _atr_ratio(frame: MarketDataFrame | None, value: float) -> float | None:
    if not frame:
        return None
    ranges = [candle.high - candle.low for candle in frame.closed_candles()[-20:]]
    average = sum(ranges) / len(ranges) if ranges else 0
    return value / average if average else None


def _range_edge_distance(frame: MarketDataFrame, price: float) -> float | None:
    candles = frame.closed_candles()[-50:]
    if not candles:
        return None
    high = max(candle.high for candle in candles)
    low = min(candle.low for candle in candles)
    if high <= low:
        return None
    return min(abs(price - high), abs(price - low)) / (high - low) * 100


def _nearest_htf_liquidity_distance(review: dict[str, Any], price: float) -> float | None:
    htf = [item for item in review.get("Liquidity", []) if item.get("timeframe") in {"D1", "H4"}]
    if not htf:
        return None
    return min(abs(price - float(item["price"])) / price * 100 for item in htf)


def _extension_atr(frame: MarketDataFrame, timestamp: int | None) -> float | None:
    if timestamp is None:
        return None
    candles = frame.closed_candles()
    origin = next((candle for candle in candles if candle.timestamp >= timestamp), None)
    if origin is None:
        return None
    ranges = [candle.high - candle.low for candle in candles[-20:]]
    average = sum(ranges) / len(ranges) if ranges else 0
    if not average:
        return None
    return abs(candles[-1].close - origin.close) / average


def _short_long_volatility_ratio(frame: MarketDataFrame) -> float | None:
    candles = frame.closed_candles()
    if len(candles) < 50:
        return None
    short_ranges = [candle.high - candle.low for candle in candles[-10:]]
    long_ranges = [candle.high - candle.low for candle in candles[-50:]]
    short = sum(short_ranges) / len(short_ranges)
    long = sum(long_ranges) / len(long_ranges)
    return short / long if long else None


def _session(timestamp: int) -> str:
    hour = (timestamp // 3600) % 24
    if 0 <= hour < 7:
        return "ASIA"
    if 7 <= hour < 13:
        return "LONDON"
    if 13 <= hour < 20:
        return "NEW_YORK"
    return "LATE_US"


def _trend_maturity(alignment_count: Any, phase: Any) -> str:
    if alignment_count == 2 and phase == "CONTINUATION":
        return "MATURE_CONTINUATION"
    if alignment_count == 2 and phase == "PULLBACK":
        return "ALIGNED_PULLBACK"
    if alignment_count == 1:
        return "MIXED_HTF"
    return "UNALIGNED_OR_RANGE"


def _freshness_status(age_bars: Any, touch_count: Any) -> str:
    if age_bars is None:
        return "UNAVAILABLE"
    if (touch_count or 0) == 0 and age_bars <= 24:
        return "FRESH"
    if (touch_count or 0) <= 2 and age_bars <= 72:
        return "LIGHTLY_TESTED"
    return "AGED"


def _poi_freshness_status(age_bars: Any, setup: dict[str, Any] | None) -> str:
    if not setup or age_bars is None:
        return "UNAVAILABLE"
    if setup.get("status") == "FRESH":
        return "FRESH"
    if setup.get("status") in {"TOUCHED", "PARTIALLY_MITIGATED"}:
        return "LIGHTLY_TESTED"
    return "AGED"


def _unavailable(availability: dict[str, str], key: str, reason: str) -> None:
    availability[key] = reason
    return None


def _int_or_none(value: Any) -> int | None:
    if value in {None, "NONE"}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
