"""External fetch and immutable market-data generation."""

from __future__ import annotations

import json
from pathlib import Path

from .model import (
    ACTIVE_SYMBOLS,
    TIMEFRAME_SECONDS,
    Candle,
    new_generation_id,
    utc_now_iso,
    validate_generation,
)
from .providers import MarketDataProvider, choose_complete_provider


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
    for symbol in ACTIVE_SYMBOLS:
        provider, raw_frames = choose_complete_provider(symbol, providers)
        snapshot[symbol] = build_symbol_generation(symbol, provider, raw_frames, fetch_timestamp)
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
        validate_generation({tf: __import__("market_reviewer.model", fromlist=["to_market_data_frame"]).to_market_data_frame(raw) for tf, raw in frames.items()})
    path = output_dir / "market-data-v1.json"
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    return path
