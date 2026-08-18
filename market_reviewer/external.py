"""External fetch and immutable market-data generation."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .model import (
    ACTIVE_SYMBOLS,
    DataUnavailable,
    TIMEFRAME_SECONDS,
    Candle,
    new_generation_id,
    to_market_data_frame,
    utc_now_iso,
    validate_generation,
)
from .providers import CRYPTO_PROVIDER_ORDER, MarketDataProvider, default_crypto_providers


def closed_candle_timestamp(candle: Candle, timeframe: str, fetch_timestamp: int) -> int | None:
    duration = TIMEFRAME_SECONDS[timeframe]
    return candle.timestamp if candle.timestamp + duration <= fetch_timestamp else None


def build_symbol_generation(
    symbol: str,
    provider: MarketDataProvider,
    raw_frames: dict[str, list[Candle]],
    fetch_timestamp: int,
    source_environment: str = "github_actions",
) -> dict[str, dict]:
    generation_id = new_generation_id()
    generated_at = utc_now_iso()
    staged: dict[str, dict] = {}
    for timeframe, candles in raw_frames.items():
        closed = [c for c in candles if closed_candle_timestamp(c, timeframe, fetch_timestamp) is not None]
        current_open = None
        if len(closed) < len(candles):
            current_open = candles[len(closed)].timestamp
        latest_closed = closed[-1].timestamp if closed else 0
        staged[timeframe] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "source": "external_fetch",
            "provider": provider.name,
            "market_type": "crypto_perpetual" if provider.name == "bitunix_perpetual" else "crypto_spot",
            "timezone": "UTC",
            "dataset_id": f"market-data.v1:{symbol}:{timeframe}",
            "generation_id": generation_id,
            "generated_at": generated_at,
            "source_environment": source_environment,
            "completeness_status": "DATA_READY",
            "fetch_timestamp": fetch_timestamp,
            "latest_candle_timestamp": candles[-1].timestamp,
            "latest_closed_candle_timestamp": latest_closed,
            "current_open_candle_timestamp": current_open,
            "OHLCV": [c.__dict__ for c in candles],
            "status": "DATA_READY",
            "warnings": [],
        }
    return staged


def fetch_external_generation(
    providers: list[MarketDataProvider],
    fetch_timestamp: int,
) -> dict[str, dict[str, dict]]:
    snapshot: dict[str, dict[str, dict]] = {}
    ordered = sorted(
        providers,
        key=lambda provider: CRYPTO_PROVIDER_ORDER.index(provider.name)
        if provider.name in CRYPTO_PROVIDER_ORDER
        else len(CRYPTO_PROVIDER_ORDER),
    )
    for symbol in ACTIVE_SYMBOLS:
        for provider in ordered:
            raw_frames = {timeframe: provider.fetch_ohlcv(symbol, timeframe) for timeframe in TIMEFRAME_SECONDS}
            if any(not candles for candles in raw_frames.values()):
                continue
            staged = build_symbol_generation(symbol, provider, raw_frames, fetch_timestamp)
            try:
                validate_generation({tf: to_market_data_frame(raw) for tf, raw in staged.items()})
            except DataUnavailable:
                continue
            snapshot[symbol] = staged
            break
        if symbol not in snapshot:
            raise RuntimeError("DATA_UNAVAILABLE")
    return snapshot


def latest_closed_index(snapshot: dict[str, dict[str, dict]]) -> dict[str, dict[str, int]]:
    return {
        symbol: {
            timeframe: frame["latest_closed_candle_timestamp"]
            for timeframe, frame in frames.items()
        }
        for symbol, frames in snapshot.items()
    }


def has_new_closed_candle(
    previous: dict[str, dict[str, int]] | None,
    current: dict[str, dict[str, int]],
) -> bool:
    if previous is None:
        return True
    for symbol, frames in current.items():
        for timeframe, timestamp in frames.items():
            if timestamp != previous.get(symbol, {}).get(timeframe):
                return True
    return False


def publish_artifact(snapshot: dict[str, dict[str, dict]], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    for symbol, frames in snapshot.items():
        validate_generation({tf: to_market_data_frame(raw) for tf, raw in frames.items()})
    path = output_dir / "market-data-v1.json"
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    return path


def run_external_fetch(output_dir: Path, fetch_timestamp: int | None = None) -> Path:
    timestamp = int(time.time()) if fetch_timestamp is None else fetch_timestamp
    snapshot = fetch_external_generation(default_crypto_providers(), timestamp)
    return publish_artifact(snapshot, output_dir)
