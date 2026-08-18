"""Market-data schema and validation primitives."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4


TIMEFRAMES = ("D1", "H4", "H1", "M15", "M5")
ACTIVE_SYMBOLS = ("BTC", "ETH")
MIN_CLOSED_CANDLES = 50
TIMEFRAME_SECONDS = {
    "D1": 86_400,
    "H4": 14_400,
    "H1": 3_600,
    "M15": 900,
    "M5": 300,
}


class DataUnavailable(ValueError):
    """Raised when data may not be used for review."""


@dataclass(frozen=True)
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    @classmethod
    def from_mapping(cls, value: dict[str, Any]) -> "Candle":
        return cls(
            timestamp=int(value["timestamp"]),
            open=float(value["open"]),
            high=float(value["high"]),
            low=float(value["low"]),
            close=float(value["close"]),
            volume=float(value.get("volume", 0)),
        )

    def validate(self, fetch_timestamp: int) -> None:
        if self.timestamp > fetch_timestamp:
            raise DataUnavailable("future timestamp veto")
        if min(self.open, self.high, self.low, self.close) <= 0:
            raise DataUnavailable("invalid OHLC")
        if self.high < max(self.open, self.close):
            raise DataUnavailable("high below open/close")
        if self.low > min(self.open, self.close):
            raise DataUnavailable("low above open/close")
        if self.volume < 0:
            raise DataUnavailable("negative volume")


@dataclass
class MarketDataFrame:
    symbol: str
    timeframe: str
    source: str
    provider: str
    market_type: str
    timezone: str
    dataset_id: str
    generation_id: str
    generated_at: str
    source_environment: str
    completeness_status: str
    fetch_timestamp: int
    latest_candle_timestamp: int
    latest_closed_candle_timestamp: int
    current_open_candle_timestamp: int | None
    candles: list[Candle]
    status: str
    warnings: list[str] = field(default_factory=list)

    def closed_candles(self) -> list[Candle]:
        return [c for c in self.candles if c.timestamp <= self.latest_closed_candle_timestamp]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_generation_id() -> str:
    return str(uuid4())


def to_market_data_frame(raw: dict[str, Any]) -> MarketDataFrame:
    required = {
        "symbol",
        "timeframe",
        "source",
        "provider",
        "market_type",
        "timezone",
        "dataset_id",
        "generation_id",
        "generated_at",
        "source_environment",
        "completeness_status",
        "fetch_timestamp",
        "latest_candle_timestamp",
        "latest_closed_candle_timestamp",
        "OHLCV",
        "status",
        "warnings",
    }
    missing = sorted(required.difference(raw))
    if missing:
        raise DataUnavailable(f"required metadata missing: {', '.join(missing)}")
    current_open = raw.get("current_open_candle_timestamp")
    return MarketDataFrame(
        symbol=str(raw["symbol"]),
        timeframe=str(raw["timeframe"]),
        source=str(raw["source"]),
        provider=str(raw["provider"]),
        market_type=str(raw["market_type"]),
        timezone=str(raw["timezone"]),
        dataset_id=str(raw["dataset_id"]),
        generation_id=str(raw["generation_id"]),
        generated_at=str(raw["generated_at"]),
        source_environment=str(raw["source_environment"]),
        completeness_status=str(raw["completeness_status"]),
        fetch_timestamp=int(raw["fetch_timestamp"]),
        latest_candle_timestamp=int(raw["latest_candle_timestamp"]),
        latest_closed_candle_timestamp=int(raw["latest_closed_candle_timestamp"]),
        current_open_candle_timestamp=None if current_open is None else int(current_open),
        candles=[Candle.from_mapping(c) for c in raw["OHLCV"]],
        status=str(raw["status"]),
        warnings=list(raw["warnings"]),
    )


def validate_market_data_frame(frame: MarketDataFrame) -> None:
    if frame.symbol not in ACTIVE_SYMBOLS:
        raise DataUnavailable("unsupported symbol")
    if frame.timeframe not in TIMEFRAMES:
        raise DataUnavailable("unsupported timeframe")
    if frame.completeness_status != "DATA_READY" or frame.status != "DATA_READY":
        raise DataUnavailable("DATA_UNAVAILABLE")
    timestamps = [c.timestamp for c in frame.candles]
    if len(set(timestamps)) != len(timestamps):
        raise DataUnavailable("duplicate timestamps")
    if timestamps != sorted(timestamps):
        raise DataUnavailable("timestamps not strictly increasing")
    if len(frame.closed_candles()) < MIN_CLOSED_CANDLES:
        raise DataUnavailable("fewer than 50 closed candles")
    for candle in frame.candles:
        candle.validate(frame.fetch_timestamp)
    if frame.latest_candle_timestamp != timestamps[-1]:
        raise DataUnavailable("latest candle timestamp mismatch")
    if frame.latest_closed_candle_timestamp not in timestamps:
        raise DataUnavailable("closed candle timestamp missing")
    if frame.current_open_candle_timestamp is not None:
        if frame.current_open_candle_timestamp <= frame.latest_closed_candle_timestamp:
            raise DataUnavailable("open candle overlaps closed candle")
    if frame.timezone.upper() != "UTC":
        raise DataUnavailable("timezone must be UTC")


def validate_generation(frames: dict[str, MarketDataFrame]) -> None:
    expected = set(TIMEFRAMES)
    if set(frames) != expected:
        raise DataUnavailable("generation missing required timeframe")
    generation_ids = {frame.generation_id for frame in frames.values()}
    providers = {frame.provider for frame in frames.values()}
    symbols = {frame.symbol for frame in frames.values()}
    if len(symbols) != 1:
        raise DataUnavailable("generation contains multiple symbols")
    if len(generation_ids) != 1:
        raise DataUnavailable("generation id mismatch")
    if len(providers) != 1:
        raise DataUnavailable("provider mismatch")
    for frame in frames.values():
        validate_market_data_frame(frame)
