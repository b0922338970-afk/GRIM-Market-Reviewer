"""Research-only external market evidence schema.

This layer models derivatives/flow evidence for future opportunity research.
It never mutates production review state and assigns no score or weight.
"""

from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from typing import Any


EXTERNAL_EVIDENCE_SCHEMA_VERSION = "external-market-evidence.v1"

EXTERNAL_DOMAINS = {
    "CAPITAL_FLOW",
    "POSITIONING",
    "LIQUIDATION_CONTEXT",
    "CROWDING",
}
DOMAIN_CLASSIFICATIONS = {
    "POSITIVE",
    "NEUTRAL",
    "NEGATIVE",
    "RAW_DATA_AVAILABLE_BUT_UNCLASSIFIED",
    "DATA_UNAVAILABLE",
    "NOT_APPLICABLE",
}
AVAILABILITY_STATES = {
    "AVAILABLE",
    "UNAVAILABLE",
    "STALE",
    "DELAYED",
    "PARTIAL",
}
RELATION_FEATURE_STATUS = "EXPERIMENTAL"
RELATION_FEATURE_TYPES = {"OBSERVATION_FEATURE", "DECISION_FEATURE_CANDIDATE"}

EXTERNAL_RAW_METRIC_IDS = {
    "OI": ("oi", "oi_change_5m", "oi_change_15m", "oi_change_1h", "oi_change_4h"),
    "Funding": ("funding_rate", "funding_change", "funding_percentile", "next_funding_timestamp"),
    "CVD": ("spot_cvd", "spot_cvd_change", "perp_cvd", "perp_cvd_change"),
    "Liquidation": (
        "long_liquidation_notional",
        "short_liquidation_notional",
        "long_liquidation_notional_5m",
        "short_liquidation_notional_5m",
        "long_liquidation_notional_15m",
        "short_liquidation_notional_15m",
        "long_liquidation_notional_1h",
        "short_liquidation_notional_1h",
        "nearest_long_liq_cluster",
        "nearest_short_liq_cluster",
        "liq_cluster_distance_pct",
        "liq_cluster_density",
        "liq_cluster_asymmetry",
    ),
    "LargeOrderFlow": (
        "large_buy_notional",
        "large_sell_notional",
        "large_order_imbalance",
        "net_capital_flow",
        "net_capital_flow_change",
    ),
}


@dataclass(frozen=True)
class ExternalMetric:
    metric_id: str
    value: Any
    source: str
    source_timestamp: int | None
    available_at: int | None
    time_window: str
    availability: str
    evidence_id: str
    provider: str
    market_type: str
    symbol: str
    instrument: str
    fetch_timestamp: int | None
    raw_unit: str
    normalized_unit: str
    raw_value: Any
    semantics: str


def unavailable_metric(metric_id: str, source: str = "NONE", time_window: str = "NONE") -> dict[str, Any]:
    return asdict(ExternalMetric(metric_id, None, source, None, None, time_window, "UNAVAILABLE", f"UNAVAILABLE:{metric_id}", source, "NONE", "NONE", "NONE", None, "NONE", "NONE", None, "OBSERVED"))


def metric(
    metric_id: str,
    value: Any,
    source: str,
    source_timestamp: int,
    available_at: int,
    time_window: str,
    availability: str = "AVAILABLE",
    evidence_id: str | None = None,
    provider: str | None = None,
    market_type: str = "UNKNOWN",
    symbol: str = "UNKNOWN",
    instrument: str = "UNKNOWN",
    fetch_timestamp: int | None = None,
    raw_unit: str = "UNKNOWN",
    normalized_unit: str = "UNKNOWN",
    raw_value: Any = None,
    semantics: str = "OBSERVED",
) -> dict[str, Any]:
    if availability not in AVAILABILITY_STATES:
        raise ValueError(f"invalid availability: {availability}")
    return asdict(
        ExternalMetric(
            metric_id=metric_id,
            value=value,
            source=source,
            source_timestamp=source_timestamp,
            available_at=available_at,
            time_window=time_window,
            availability=availability,
            evidence_id=evidence_id or f"{source}:{metric_id}:{source_timestamp}",
            provider=provider or source,
            market_type=market_type,
            symbol=symbol,
            instrument=instrument,
            fetch_timestamp=fetch_timestamp,
            raw_unit=raw_unit,
            normalized_unit=normalized_unit,
            raw_value=value if raw_value is None else raw_value,
            semantics=semantics,
        )
    )


def build_external_market_evidence(
    symbol: str,
    snapshot_timestamp: int,
    metrics: dict[str, dict[str, Any]] | None = None,
    context: dict[str, Any] | None = None,
    stale_after_seconds: int | None = None,
) -> dict[str, Any]:
    provided = metrics or {}
    normalized = _normalize_metrics(provided, snapshot_timestamp, stale_after_seconds)
    relation_features = relation_features_from_metrics(normalized, context or {}, snapshot_timestamp)
    domain_classification = classify_external_domains(normalized, relation_features)
    return {
        "schema_version": EXTERNAL_EVIDENCE_SCHEMA_VERSION,
        "symbol": symbol,
        "snapshot_timestamp": snapshot_timestamp,
        "raw_metrics": normalized,
        "relation_features": relation_features,
        "domain_classification": domain_classification,
        "external_health": external_health(normalized, relation_features),
    }


def relation_features_from_metrics(
    metrics: dict[str, dict[str, Any]],
    context: dict[str, Any],
    snapshot_timestamp: int,
) -> dict[str, dict[str, Any]]:
    features = {}
    generic_price_change = _number(context.get("price_change_pct"))
    price_change, oi_change = _aligned_price_oi_change(metrics, context)
    features["OI_PRICE_RELATION"] = _feature("OI_PRICE_RELATION", "POSITIONING", "OBSERVATION_FEATURE", classify_oi_price_relation(price_change, oi_change), snapshot_timestamp, _evidence(metrics, ("oi_change_5m", "oi_change_15m", "oi_change_1h", "oi_change_4h")))

    spot_change = _available_number(metrics.get("spot_cvd_change"))
    perp_change = _available_number(metrics.get("perp_cvd_change"))
    features["SPOT_PERP_CVD_RELATION"] = _feature("SPOT_PERP_CVD_RELATION", "CAPITAL_FLOW", "DECISION_FEATURE_CANDIDATE", classify_cvd_relation(spot_change, perp_change), snapshot_timestamp, _evidence(metrics, ("spot_cvd_change", "perp_cvd_change")))

    funding_rate = _available_number(metrics.get("funding_rate"))
    funding_percentile = _available_number(metrics.get("funding_percentile"))
    features["FUNDING_CROWDING_RELATION"] = _feature("FUNDING_CROWDING_RELATION", "CROWDING", "OBSERVATION_FEATURE", classify_funding_relation(generic_price_change, funding_rate, funding_percentile), snapshot_timestamp, _evidence(metrics, ("funding_rate", "funding_percentile", "funding_change")))

    cluster_distance = _available_number(metrics.get("liq_cluster_distance_pct"))
    liq_asymmetry = _available_number(metrics.get("liq_cluster_asymmetry"))
    long_liq = _first_number(metrics, ("long_liquidation_notional_5m", "long_liquidation_notional_15m", "long_liquidation_notional_1h", "long_liquidation_notional"))
    short_liq = _first_number(metrics, ("short_liquidation_notional_5m", "short_liquidation_notional_15m", "short_liquidation_notional_1h", "short_liquidation_notional"))
    liquidation_coverage = _liquidation_coverage_status(metrics)
    features["LIQUIDATION_CONTEXT_RELATION"] = _feature("LIQUIDATION_CONTEXT_RELATION", "LIQUIDATION_CONTEXT", "OBSERVATION_FEATURE", classify_liquidation_relation(cluster_distance, liq_asymmetry, long_liq, short_liq, liquidation_coverage), snapshot_timestamp, _evidence(metrics, ("liq_cluster_distance_pct", "liq_cluster_asymmetry", "long_liquidation_notional_5m", "short_liquidation_notional_5m", "long_liquidation_notional_15m", "short_liquidation_notional_15m", "long_liquidation_notional_1h", "short_liquidation_notional_1h", "long_liquidation_notional", "short_liquidation_notional")))

    buy = _available_number(metrics.get("large_buy_notional"))
    sell = _available_number(metrics.get("large_sell_notional"))
    imbalance = _available_number(metrics.get("large_order_imbalance"))
    features["LARGE_ORDER_FLOW_RELATION"] = _feature("LARGE_ORDER_FLOW_RELATION", "CAPITAL_FLOW", "DECISION_FEATURE_CANDIDATE", classify_large_order_relation(generic_price_change, buy, sell, imbalance), snapshot_timestamp, _evidence(metrics, ("large_buy_notional", "large_sell_notional", "large_order_imbalance", "net_capital_flow")))
    return features


def classify_oi_price_relation(price_change_pct: float | None, oi_change: float | None) -> str:
    if oi_change is None:
        return "DATA_UNAVAILABLE"
    if price_change_pct is None:
        return "RAW_DATA_AVAILABLE_BUT_UNCLASSIFIED"
    if price_change_pct > 0 and oi_change > 0:
        return "PRICE_UP_OI_UP"
    if price_change_pct > 0 and oi_change < 0:
        return "PRICE_UP_OI_DOWN"
    if price_change_pct < 0 and oi_change > 0:
        return "PRICE_DOWN_OI_UP"
    if price_change_pct < 0 and oi_change < 0:
        return "PRICE_DOWN_OI_DOWN"
    if price_change_pct == 0 and oi_change > 0:
        return "PRICE_FLAT_OI_UP"
    if price_change_pct == 0 and oi_change < 0:
        return "PRICE_FLAT_OI_DOWN"
    return "PRICE_OI_NEUTRAL"


def classify_cvd_relation(spot_cvd_change: float | None, perp_cvd_change: float | None) -> str:
    if spot_cvd_change is None or perp_cvd_change is None:
        return "DATA_UNAVAILABLE"
    if spot_cvd_change > 0 and perp_cvd_change > 0:
        return "SPOT_PERP_CONFIRMATION"
    if spot_cvd_change > 0 and perp_cvd_change <= 0:
        return "SPOT_LED_BUYING"
    if perp_cvd_change > 0 and spot_cvd_change <= 0:
        return "PERP_LED_BUYING"
    if spot_cvd_change * perp_cvd_change < 0:
        return "SPOT_PERP_DIVERGENCE"
    return "NEUTRAL"


def classify_funding_relation(price_change_pct: float | None, funding_rate: float | None, funding_percentile: float | None) -> str:
    if funding_rate is None and funding_percentile is None:
        return "DATA_UNAVAILABLE"
    percentile = funding_percentile if funding_percentile is not None else 50
    if percentile >= 90 or (funding_rate is not None and funding_rate > 0.0005):
        return "LONG_CROWDING"
    if percentile <= 10 or (funding_rate is not None and funding_rate < -0.0005):
        return "SHORT_CROWDING"
    if price_change_pct is not None and price_change_pct > 0 and funding_rate is not None and funding_rate < 0:
        return "FUNDING_PRICE_DIVERGENCE"
    return "HEALTHY_FUNDING"


def classify_liquidation_relation(
    cluster_distance_pct: float | None,
    cluster_asymmetry: float | None,
    long_liquidation_notional: float | None,
    short_liquidation_notional: float | None,
    coverage_status: str | None = None,
) -> str:
    if all(value is None for value in (cluster_distance_pct, cluster_asymmetry, long_liquidation_notional, short_liquidation_notional)):
        return "DATA_UNAVAILABLE"
    if long_liquidation_notional and long_liquidation_notional > 0:
        return "LONG_LIQUIDATION_FLUSH"
    if short_liquidation_notional and short_liquidation_notional > 0:
        return "SHORT_LIQUIDATION_SQUEEZE"
    if cluster_distance_pct is not None and cluster_distance_pct <= 0:
        return "LIQ_CLUSTER_SWEPT"
    if cluster_distance_pct is not None and cluster_distance_pct < 1:
        return "LIQ_CLUSTER_APPROACH"
    if cluster_asymmetry is not None:
        return "NEUTRAL"
    if long_liquidation_notional == 0 and short_liquidation_notional == 0:
        return "NEUTRAL" if coverage_status in {None, "COMPLETE", "AVAILABLE"} else "DATA_UNAVAILABLE"
    return "DATA_UNAVAILABLE"


def classify_large_order_relation(
    price_change_pct: float | None,
    large_buy_notional: float | None,
    large_sell_notional: float | None,
    large_order_imbalance: float | None,
) -> str:
    if large_buy_notional is None and large_sell_notional is None and large_order_imbalance is None:
        return "DATA_UNAVAILABLE"
    imbalance = large_order_imbalance
    if imbalance is None and large_buy_notional is not None and large_sell_notional is not None:
        total = large_buy_notional + large_sell_notional
        imbalance = (large_buy_notional - large_sell_notional) / total if total else 0
    if imbalance is None or price_change_pct is None:
        return "NEUTRAL"
    if imbalance > 0 and price_change_pct > 0:
        return "LARGE_BUY_WITH_PRICE_PROGRESS"
    if imbalance > 0 and price_change_pct <= 0:
        return "LARGE_BUY_ABSORBED"
    if imbalance < 0 and price_change_pct < 0:
        return "LARGE_SELL_WITH_PRICE_PROGRESS"
    if imbalance < 0 and price_change_pct >= 0:
        return "LARGE_SELL_ABSORBED"
    return "NEUTRAL"


def classify_external_domains(metrics: dict[str, dict[str, Any]], features: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        "CAPITAL_FLOW": _domain("CAPITAL_FLOW", [features["SPOT_PERP_CVD_RELATION"], features["LARGE_ORDER_FLOW_RELATION"]]),
        "POSITIONING": _domain("POSITIONING", [features["OI_PRICE_RELATION"]]),
        "LIQUIDATION_CONTEXT": _domain("LIQUIDATION_CONTEXT", [features["LIQUIDATION_CONTEXT_RELATION"]]),
        "CROWDING": _domain("CROWDING", [features["FUNDING_CROWDING_RELATION"]]),
    }


def attach_external_evidence_reference(opportunity_snapshot: dict[str, Any], external_evidence: dict[str, Any]) -> dict[str, Any]:
    snapshot = copy.deepcopy(opportunity_snapshot)
    if external_evidence.get("schema_version") != EXTERNAL_EVIDENCE_SCHEMA_VERSION:
        raise ValueError("unsupported external evidence schema")
    if external_evidence.get("snapshot_timestamp") != snapshot.get("snapshot_timestamp"):
        raise ValueError("external evidence snapshot timestamp mismatch")
    snapshot["external_evidence_ref"] = {
        "schema_version": external_evidence["schema_version"],
        "symbol": external_evidence["symbol"],
        "snapshot_timestamp": external_evidence["snapshot_timestamp"],
    }
    return snapshot


def _normalize_metrics(
    provided: dict[str, dict[str, Any]],
    snapshot_timestamp: int,
    stale_after_seconds: int | None,
) -> dict[str, dict[str, Any]]:
    normalized = {}
    for metric_id in [metric for metrics in EXTERNAL_RAW_METRIC_IDS.values() for metric in metrics]:
        item = dict(provided.get(metric_id) or unavailable_metric(metric_id))
        _validate_metric_time(item, snapshot_timestamp)
        if item["availability"] == "AVAILABLE" and stale_after_seconds is not None and item.get("available_at") is not None:
            if snapshot_timestamp - int(item["available_at"]) > stale_after_seconds:
                item["availability"] = "STALE"
        normalized[metric_id] = item
    return normalized


def _validate_metric_time(item: dict[str, Any], snapshot_timestamp: int) -> None:
    source_timestamp = item.get("source_timestamp")
    available_at = item.get("available_at")
    if source_timestamp is not None and int(source_timestamp) > snapshot_timestamp:
        raise ValueError(f"source timestamp after snapshot: {item.get('metric_id')}")
    if available_at is not None and int(available_at) > snapshot_timestamp:
        raise ValueError(f"available_at after snapshot: {item.get('metric_id')}")
    if item.get("availability") not in AVAILABILITY_STATES:
        raise ValueError(f"invalid availability: {item.get('availability')}")


def _feature(feature_id: str, domain: str, feature_type: str, relation: str, snapshot_timestamp: int, evidence: list[str]) -> dict[str, Any]:
    if feature_type not in RELATION_FEATURE_TYPES:
        raise ValueError(f"invalid relation feature type: {feature_type}")
    return {
        "feature_id": feature_id,
        "feature_type": feature_type,
        "domain": domain,
        "status": RELATION_FEATURE_STATUS,
        "version": 1,
        "available_at": snapshot_timestamp,
        "source_evidence_ids": evidence,
        "value": {"relation": relation},
    }


def _domain(domain: str, features: list[dict[str, Any]]) -> dict[str, Any]:
    relations = [feature["value"]["relation"] for feature in features]
    evidence_ids = [evidence_id for feature in features for evidence_id in feature["source_evidence_ids"]]
    available = [relation for relation in relations if relation != "DATA_UNAVAILABLE"]
    if not available:
        classification = "DATA_UNAVAILABLE"
    elif all(relation == "RAW_DATA_AVAILABLE_BUT_UNCLASSIFIED" for relation in available):
        classification = "RAW_DATA_AVAILABLE_BUT_UNCLASSIFIED"
    elif any(relation in {"LONG_CROWDING", "SHORT_CROWDING", "SPOT_PERP_DIVERGENCE", "LARGE_BUY_ABSORBED", "LARGE_SELL_ABSORBED", "PRICE_DOWN_OI_UP"} for relation in available):
        classification = "NEGATIVE"
    elif any(relation in {"PRICE_UP_OI_UP", "LIQ_CLUSTER_APPROACH", "LIQ_CLUSTER_SWEPT", "LONG_LIQUIDATION_FLUSH", "SHORT_LIQUIDATION_SQUEEZE", "SPOT_PERP_CONFIRMATION", "SPOT_LED_BUYING", "PERP_LED_BUYING", "LARGE_BUY_WITH_PRICE_PROGRESS", "LARGE_SELL_WITH_PRICE_PROGRESS"} for relation in available):
        classification = "POSITIVE"
    else:
        classification = "NEUTRAL"
    return {"domain": domain, "classification": classification, "source_evidence_ids": evidence_ids, "relations": relations}


def external_health(metrics: dict[str, dict[str, Any]], features: dict[str, dict[str, Any]]) -> dict[str, int]:
    raw_available = sum(1 for item in metrics.values() if item.get("availability") == "AVAILABLE")
    raw_unavailable = sum(1 for item in metrics.values() if item.get("availability") != "AVAILABLE")
    relations = [feature["value"]["relation"] for feature in features.values()]
    data_unavailable = sum(1 for relation in relations if relation == "DATA_UNAVAILABLE")
    unclassified = sum(1 for relation in relations if relation == "RAW_DATA_AVAILABLE_BUT_UNCLASSIFIED")
    return {
        "raw_available": raw_available,
        "raw_unavailable": raw_unavailable,
        "classified": len(relations) - data_unavailable - unclassified,
        "unclassified_available": unclassified,
        "data_unavailable": data_unavailable,
    }


def _aligned_price_oi_change(metrics: dict[str, dict[str, Any]], context: dict[str, Any]) -> tuple[float | None, float | None]:
    pairs = (
        ("oi_change_5m", "price_change_5m_pct"),
        ("oi_change_15m", "price_change_15m_pct"),
        ("oi_change_1h", "price_change_1h_pct"),
        ("oi_change_4h", "price_change_4h_pct"),
    )
    for oi_key, price_key in pairs:
        oi_change = _available_number(metrics.get(oi_key))
        if oi_change is not None:
            price_change = _number(context.get(price_key))
            if price_change is None and "price_change_pct" in context:
                price_change = _number(context.get("price_change_pct"))
            return price_change, oi_change
    return None, None


def _liquidation_coverage_status(metrics: dict[str, dict[str, Any]]) -> str:
    for window in ("5m", "15m", "1h", ""):
        suffix = f"_{window}" if window else ""
        long_item = metrics.get(f"long_liquidation_notional{suffix}")
        short_item = metrics.get(f"short_liquidation_notional{suffix}")
        if not long_item or not short_item:
            continue
        if long_item.get("availability") == "AVAILABLE" and short_item.get("availability") == "AVAILABLE":
            return str(long_item.get("coverage_status") or short_item.get("coverage_status") or "COMPLETE")
        return str(long_item.get("coverage_status") or short_item.get("coverage_status") or "UNAVAILABLE")
    return "UNAVAILABLE"


def _available_number(item: dict[str, Any] | None) -> float | None:
    if not item or item.get("availability") != "AVAILABLE":
        return None
    return _number(item.get("value"))


def _first_number(metrics: dict[str, dict[str, Any]], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _available_number(metrics.get(key))
        if value is not None:
            return value
    return None


def _number(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _evidence(metrics: dict[str, dict[str, Any]], keys: tuple[str, ...]) -> list[str]:
    return [
        str(metrics[key]["evidence_id"])
        for key in keys
        if key in metrics and metrics[key].get("availability") == "AVAILABLE"
    ]
