"""Provider adapters for research-only external market evidence.

Phase 1 supports derivatives OI, funding, and actual liquidation-flow
normalization without touching production review state.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

from .external_evidence import build_external_market_evidence, metric, unavailable_metric
from .model import ACTIVE_SYMBOLS
from .providers import _get_json


EXTERNAL_EVIDENCE_ARTIFACT_VERSION = "external-market-evidence-artifact.v1"
DEFAULT_EXTERNAL_PROVIDER = "binance_usdm_futures"
LIQUIDATION_HISTORY_LIVE_ONLY = "LIVE_ONLY"

INSTRUMENT_MAPPING = {
    "BTC": {
        "provider": DEFAULT_EXTERNAL_PROVIDER,
        "instrument": "BTCUSDT",
        "market_type": "usdt_perpetual",
    },
    "ETH": {
        "provider": DEFAULT_EXTERNAL_PROVIDER,
        "instrument": "ETHUSDT",
        "market_type": "usdt_perpetual",
    },
}

FRESHNESS_SECONDS = {
    "oi": 300,
    "oi_change_5m": 600,
    "oi_change_15m": 1_200,
    "oi_change_1h": 4_200,
    "oi_change_4h": 15_000,
    "funding_rate": 32_400,
    "funding_change": 32_400,
    "funding_percentile": 604_800,
    "next_funding_timestamp": 32_400,
    "long_liquidation_notional_5m": 300,
    "short_liquidation_notional_5m": 300,
    "long_liquidation_notional_15m": 900,
    "short_liquidation_notional_15m": 900,
    "long_liquidation_notional_1h": 3_600,
    "short_liquidation_notional_1h": 3_600,
}

FUNDING_HISTORY_MINIMUM_FOR_PERCENTILE = 30


class ExternalEvidenceUnavailable(RuntimeError):
    """Raised when an adapter cannot fetch any external evidence."""


@dataclass(frozen=True)
class InstrumentMapping:
    symbol: str
    provider: str
    instrument: str
    market_type: str


def instrument_mapping(symbol: str) -> InstrumentMapping:
    if symbol not in INSTRUMENT_MAPPING:
        raise ValueError(f"unsupported external evidence symbol: {symbol}")
    value = INSTRUMENT_MAPPING[symbol]
    return InstrumentMapping(symbol, value["provider"], value["instrument"], value["market_type"])


def phase1_metric(
    metric_id: str,
    value: Any,
    mapping: InstrumentMapping,
    source_timestamp: int,
    available_at: int,
    fetch_timestamp: int,
    time_window: str,
    raw_unit: str,
    normalized_unit: str,
    raw_value: Any = None,
    availability: str = "AVAILABLE",
    semantics: str = "OBSERVED",
) -> dict[str, Any]:
    item = metric(
        metric_id,
        value,
        mapping.provider,
        source_timestamp,
        available_at,
        time_window,
        availability=availability,
        evidence_id=f"{mapping.provider}:{mapping.instrument}:{metric_id}:{source_timestamp}",
        provider=mapping.provider,
        market_type=mapping.market_type,
        symbol=mapping.symbol,
        instrument=mapping.instrument,
        fetch_timestamp=fetch_timestamp,
        raw_unit=raw_unit,
        normalized_unit=normalized_unit,
        raw_value=value if raw_value is None else raw_value,
        semantics=semantics,
    )
    return apply_metric_freshness(item, fetch_timestamp)


def unavailable_phase1_metric(metric_id: str, mapping: InstrumentMapping, time_window: str = "NONE") -> dict[str, Any]:
    item = unavailable_metric(metric_id, mapping.provider, time_window)
    item.update(
        {
            "provider": mapping.provider,
            "market_type": mapping.market_type,
            "symbol": mapping.symbol,
            "instrument": mapping.instrument,
            "raw_unit": "NONE",
            "normalized_unit": "NONE",
        }
    )
    return item


def apply_metric_freshness(item: dict[str, Any], snapshot_timestamp: int) -> dict[str, Any]:
    if item.get("availability") != "AVAILABLE":
        return item
    threshold = FRESHNESS_SECONDS.get(str(item.get("metric_id")))
    available_at = item.get("available_at")
    if threshold is not None and available_at is not None and snapshot_timestamp - int(available_at) > threshold:
        item = dict(item)
        item["availability"] = "STALE"
    return item


def normalize_open_interest(
    mapping: InstrumentMapping,
    current_payload: dict[str, Any] | None,
    history_payload: list[dict[str, Any]] | None,
    fetch_timestamp: int,
) -> dict[str, dict[str, Any]]:
    metrics: dict[str, dict[str, Any]] = {}
    rows = sorted(history_payload or [], key=lambda row: int(row.get("timestamp", 0)))
    if current_payload:
        timestamp = int(current_payload.get("time") or current_payload.get("timestamp") or fetch_timestamp * 1000) // 1000
        raw_value = _float(current_payload.get("openInterest"))
        notional = _float(current_payload.get("sumOpenInterestValue"))
        value = notional if notional is not None else raw_value
        normalized_unit = "USD_NOTIONAL" if notional is not None else "PROVIDER_CONTRACTS"
        raw_unit = "PROVIDER_CONTRACTS"
        metrics["oi"] = phase1_metric("oi", value, mapping, timestamp, timestamp, fetch_timestamp, "current", raw_unit, normalized_unit, raw_value=raw_value)
    else:
        metrics["oi"] = unavailable_phase1_metric("oi", mapping, "current")

    latest = _oi_row_value(rows[-1]) if rows else None
    latest_ts = _row_timestamp(rows[-1]) if rows else None
    for metric_id, periods_back, window in (
        ("oi_change_5m", 1, "5m"),
        ("oi_change_15m", 3, "15m"),
        ("oi_change_1h", 12, "1h"),
        ("oi_change_4h", 48, "4h"),
    ):
        if latest is None or latest_ts is None or len(rows) <= periods_back:
            metrics[metric_id] = unavailable_phase1_metric(metric_id, mapping, window)
            continue
        previous = _oi_row_value(rows[-1 - periods_back])
        if previous is None:
            metrics[metric_id] = unavailable_phase1_metric(metric_id, mapping, window)
            continue
        metrics[metric_id] = phase1_metric(metric_id, latest - previous, mapping, latest_ts, latest_ts, fetch_timestamp, window, "USD_NOTIONAL_OR_CONTRACTS", "DELTA_NATIVE_SERIES", raw_value={"current": latest, "previous": previous})
    return metrics


def normalize_funding(
    mapping: InstrumentMapping,
    premium_payload: dict[str, Any] | None,
    funding_history_payload: list[dict[str, Any]] | None,
    fetch_timestamp: int,
) -> dict[str, dict[str, Any]]:
    metrics: dict[str, dict[str, Any]] = {}
    history = sorted(funding_history_payload or [], key=lambda row: int(row.get("fundingTime", 0)))
    if premium_payload and premium_payload.get("lastFundingRate") is not None:
        timestamp = int(premium_payload.get("time") or fetch_timestamp * 1000) // 1000
        metrics["funding_rate"] = phase1_metric("funding_rate", _float(premium_payload.get("lastFundingRate")), mapping, timestamp, timestamp, fetch_timestamp, "current", "RATE", "RATE", semantics="CURRENT_OR_PREDICTED")
    elif history:
        row = history[-1]
        timestamp = int(row["fundingTime"]) // 1000
        metrics["funding_rate"] = phase1_metric("funding_rate", _float(row.get("fundingRate")), mapping, timestamp, timestamp, fetch_timestamp, "settled", "RATE", "RATE", semantics="SETTLED")
    else:
        metrics["funding_rate"] = unavailable_phase1_metric("funding_rate", mapping, "current")

    if premium_payload and premium_payload.get("nextFundingTime") is not None:
        timestamp = int(premium_payload.get("time") or fetch_timestamp * 1000) // 1000
        next_funding = int(premium_payload["nextFundingTime"]) // 1000
        semantics = "PREDICTED" if next_funding > fetch_timestamp else "OBSERVED"
        metrics["next_funding_timestamp"] = phase1_metric("next_funding_timestamp", next_funding, mapping, timestamp, timestamp, fetch_timestamp, "next", "SECONDS", "SECONDS", semantics=semantics)
    else:
        metrics["next_funding_timestamp"] = unavailable_phase1_metric("next_funding_timestamp", mapping, "next")

    if len(history) >= 2:
        latest = _float(history[-1].get("fundingRate"))
        previous = _float(history[-2].get("fundingRate"))
        timestamp = int(history[-1]["fundingTime"]) // 1000
        metrics["funding_change"] = phase1_metric("funding_change", None if latest is None or previous is None else latest - previous, mapping, timestamp, timestamp, fetch_timestamp, "settled_delta", "RATE", "RATE_DELTA", semantics="SETTLED")
    else:
        metrics["funding_change"] = unavailable_phase1_metric("funding_change", mapping, "settled_delta")

    if len(history) >= FUNDING_HISTORY_MINIMUM_FOR_PERCENTILE:
        values = [_float(row.get("fundingRate")) for row in history]
        values = [value for value in values if value is not None]
        if values:
            latest = values[-1]
            percentile = 100 * sum(1 for value in values if value <= latest) / len(values)
            timestamp = int(history[-1]["fundingTime"]) // 1000
            metrics["funding_percentile"] = phase1_metric("funding_percentile", percentile, mapping, timestamp, timestamp, fetch_timestamp, f"{len(values)} settlements", "PERCENTILE", "PERCENTILE", semantics="DERIVED_FROM_SETTLED_HISTORY")
        else:
            metrics["funding_percentile"] = unavailable_phase1_metric("funding_percentile", mapping, "history")
    else:
        metrics["funding_percentile"] = unavailable_phase1_metric("funding_percentile", mapping, "history")
        metrics["funding_percentile"]["availability"] = "PARTIAL" if history else "UNAVAILABLE"
    return metrics


def normalize_liquidation_flow(
    mapping: InstrumentMapping,
    events: list[dict[str, Any]] | None,
    fetch_timestamp: int,
    history_capability: str = LIQUIDATION_HISTORY_LIVE_ONLY,
) -> tuple[dict[str, dict[str, Any]], str]:
    metrics: dict[str, dict[str, Any]] = {}
    if not events:
        for window in ("5m", "15m", "1h"):
            metrics[f"long_liquidation_notional_{window}"] = unavailable_phase1_metric(f"long_liquidation_notional_{window}", mapping, window)
            metrics[f"short_liquidation_notional_{window}"] = unavailable_phase1_metric(f"short_liquidation_notional_{window}", mapping, window)
        metrics["long_liquidation_notional"] = unavailable_phase1_metric("long_liquidation_notional", mapping, "current")
        metrics["short_liquidation_notional"] = unavailable_phase1_metric("short_liquidation_notional", mapping, "current")
        return metrics, history_capability

    parsed = [_parse_liquidation_event(event) for event in events]
    parsed = [event for event in parsed if event is not None and event["event_timestamp"] <= fetch_timestamp]
    latest_ts = max((event["event_timestamp"] for event in parsed), default=fetch_timestamp)
    for window, seconds in (("5m", 300), ("15m", 900), ("1h", 3600)):
        lower = fetch_timestamp - seconds
        longs = sum(event["notional"] for event in parsed if event["side"] == "LONG_LIQUIDATION" and event["event_timestamp"] > lower)
        shorts = sum(event["notional"] for event in parsed if event["side"] == "SHORT_LIQUIDATION" and event["event_timestamp"] > lower)
        metrics[f"long_liquidation_notional_{window}"] = phase1_metric(f"long_liquidation_notional_{window}", longs, mapping, latest_ts, latest_ts, fetch_timestamp, window, "USD_NOTIONAL", "USD_NOTIONAL", raw_value=longs)
        metrics[f"short_liquidation_notional_{window}"] = phase1_metric(f"short_liquidation_notional_{window}", shorts, mapping, latest_ts, latest_ts, fetch_timestamp, window, "USD_NOTIONAL", "USD_NOTIONAL", raw_value=shorts)
    metrics["long_liquidation_notional"] = metrics["long_liquidation_notional_5m"]
    metrics["short_liquidation_notional"] = metrics["short_liquidation_notional_5m"]
    return metrics, history_capability


class BinanceUSDmExternalEvidenceProvider:
    name = DEFAULT_EXTERNAL_PROVIDER
    liquidation_history = LIQUIDATION_HISTORY_LIVE_ONLY
    base_url = "https://fapi.binance.com"

    def __init__(self, get_json: Callable[[str], object] | None = None) -> None:
        self._get_json = get_json or _get_json

    def fetch_metrics(self, symbol: str, fetch_timestamp: int | None = None) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
        request_started_at = int(time.time()) if fetch_timestamp is None else fetch_timestamp
        mapping = instrument_mapping(symbol)
        if mapping.provider != self.name:
            raise ValueError(f"mapping provider mismatch: {symbol}")
        payloads: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for key, url in self._urls(mapping.instrument).items():
            try:
                payloads[key] = self._get_json(url)
            except OSError as exc:
                errors[key] = exc.__class__.__name__
        snapshot_timestamp = int(time.time()) if fetch_timestamp is None else fetch_timestamp
        metrics: dict[str, dict[str, Any]] = {}
        metrics.update(normalize_open_interest(mapping, _dict_or_none(payloads.get("open_interest")), _list_or_empty(payloads.get("open_interest_history")), snapshot_timestamp))
        metrics.update(normalize_funding(mapping, _dict_or_none(payloads.get("premium_index")), _list_or_empty(payloads.get("funding_history")), snapshot_timestamp))
        liquidation_metrics, capability = normalize_liquidation_flow(mapping, None, snapshot_timestamp, self.liquidation_history)
        metrics.update(liquidation_metrics)
        return metrics, {"provider": self.name, "instrument": mapping.instrument, "market_type": mapping.market_type, "errors": errors, "liquidation_history": capability, "request_started_at": request_started_at, "fetch_completed_at": snapshot_timestamp, "snapshot_timestamp": snapshot_timestamp}

    def _urls(self, instrument: str) -> dict[str, str]:
        return {
            "open_interest": f"{self.base_url}/fapi/v1/openInterest?{urlencode({'symbol': instrument})}",
            "open_interest_history": f"{self.base_url}/futures/data/openInterestHist?{urlencode({'symbol': instrument, 'period': '5m', 'limit': 49})}",
            "premium_index": f"{self.base_url}/fapi/v1/premiumIndex?{urlencode({'symbol': instrument})}",
            "funding_history": f"{self.base_url}/fapi/v1/fundingRate?{urlencode({'symbol': instrument, 'limit': 40})}",
        }


def build_phase1_external_evidence_artifact(
    symbols: tuple[str, ...] = ACTIVE_SYMBOLS,
    fetch_timestamp: int | None = None,
    provider: BinanceUSDmExternalEvidenceProvider | None = None,
) -> dict[str, Any]:
    adapter = provider or BinanceUSDmExternalEvidenceProvider()
    symbols_payload: dict[str, Any] = {}
    statuses = []
    for symbol in symbols:
        try:
            metrics, diagnostics = adapter.fetch_metrics(symbol, fetch_timestamp)
            snapshot_timestamp = int(diagnostics.get("snapshot_timestamp") or diagnostics.get("fetch_completed_at") or fetch_timestamp or int(time.time()))
            evidence = build_external_market_evidence(symbol, snapshot_timestamp, metrics)
            status = _phase1_status(evidence)
            diagnostics["external_evidence_status"] = status
        except Exception as exc:  # fail-open research feed boundary
            mapping = instrument_mapping(symbol)
            snapshot_timestamp = int(time.time()) if fetch_timestamp is None else fetch_timestamp
            metrics = {metric_id: unavailable_phase1_metric(metric_id, mapping) for metric_id in _phase1_metric_ids()}
            evidence = build_external_market_evidence(symbol, snapshot_timestamp, metrics)
            diagnostics = {"provider": adapter.name, "instrument": mapping.instrument, "market_type": mapping.market_type, "errors": {"adapter": exc.__class__.__name__}, "liquidation_history": getattr(adapter, "liquidation_history", "UNSUPPORTED"), "external_evidence_status": "DATA_UNAVAILABLE", "snapshot_timestamp": snapshot_timestamp, "fetch_completed_at": snapshot_timestamp}
        symbols_payload[symbol] = {"evidence": evidence, "diagnostics": diagnostics}
        statuses.append(diagnostics["external_evidence_status"])
    return {
        "artifact_version": EXTERNAL_EVIDENCE_ARTIFACT_VERSION,
        "schema_version": "external-market-evidence.v1",
        "provider": adapter.name,
        "fetch_timestamp": max(int(payload["evidence"]["snapshot_timestamp"]) for payload in symbols_payload.values()) if symbols_payload else (fetch_timestamp or int(time.time())),
        "symbols": symbols_payload,
        "external_evidence_status": "AVAILABLE" if all(status == "AVAILABLE" for status in statuses) else "PARTIAL" if any(status in {"AVAILABLE", "PARTIAL"} for status in statuses) else "DATA_UNAVAILABLE",
    }


def publish_external_evidence_artifact(artifact: dict[str, Any], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "external-market-evidence-v1.json"
    path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
    return path


def run_external_evidence_fetch(output_dir: Path, fetch_timestamp: int | None = None) -> Path:
    artifact = build_phase1_external_evidence_artifact(fetch_timestamp=fetch_timestamp)
    return publish_external_evidence_artifact(artifact, output_dir)


def _phase1_status(evidence: dict[str, Any]) -> str:
    raw = evidence["raw_metrics"]
    oi_available = any(raw[key]["availability"] == "AVAILABLE" for key in ("oi", "oi_change_5m", "oi_change_15m", "oi_change_1h", "oi_change_4h"))
    funding_available = any(raw[key]["availability"] == "AVAILABLE" for key in ("funding_rate", "funding_change", "funding_percentile", "next_funding_timestamp"))
    liquidation_available = any(raw[key]["availability"] == "AVAILABLE" for key in ("long_liquidation_notional_5m", "short_liquidation_notional_5m", "long_liquidation_notional_15m", "short_liquidation_notional_15m", "long_liquidation_notional_1h", "short_liquidation_notional_1h"))
    available_count = sum([oi_available, funding_available, liquidation_available])
    if available_count == 3:
        return "AVAILABLE"
    if available_count > 0:
        return "PARTIAL"
    return "DATA_UNAVAILABLE"


def _phase1_metric_ids() -> list[str]:
    return [
        "oi",
        "oi_change_5m",
        "oi_change_15m",
        "oi_change_1h",
        "oi_change_4h",
        "funding_rate",
        "funding_change",
        "funding_percentile",
        "next_funding_timestamp",
        "long_liquidation_notional",
        "short_liquidation_notional",
        "long_liquidation_notional_5m",
        "short_liquidation_notional_5m",
        "long_liquidation_notional_15m",
        "short_liquidation_notional_15m",
        "long_liquidation_notional_1h",
        "short_liquidation_notional_1h",
    ]


def _parse_liquidation_event(event: dict[str, Any]) -> dict[str, Any] | None:
    try:
        side = str(event["side"])
        price = float(event["price"])
        quantity = float(event["quantity"])
        timestamp = int(event["event_timestamp"])
    except (KeyError, TypeError, ValueError):
        return None
    notional = event.get("notional")
    return {
        "side": side,
        "price": price,
        "quantity": quantity,
        "notional": float(notional) if notional is not None else price * quantity,
        "event_timestamp": timestamp,
    }


def _oi_row_value(row: dict[str, Any]) -> float | None:
    value = _float(row.get("sumOpenInterestValue"))
    if value is not None:
        return value
    return _float(row.get("sumOpenInterest"))


def _row_timestamp(row: dict[str, Any]) -> int | None:
    try:
        return int(row["timestamp"]) // 1000
    except (KeyError, TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dict_or_none(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _list_or_empty(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []