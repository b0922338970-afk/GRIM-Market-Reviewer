"""Provider abstraction for standalone external market data."""

from __future__ import annotations

from typing import Protocol

from .model import Candle, TIMEFRAMES


CRYPTO_PROVIDER_ORDER = ("bitunix_perpetual", "binance", "coinbase", "kraken")


class MarketDataProvider(Protocol):
    name: str

    def fetch_ohlcv(self, symbol: str, timeframe: str) -> list[Candle]:
        """Return candles using provider candle-open timestamps."""


def choose_complete_provider(
    symbol: str,
    providers: list[MarketDataProvider],
) -> tuple[MarketDataProvider, dict[str, list[Candle]]]:
    """Pick one provider that can supply all timeframes for a symbol."""

    ordered = sorted(
        providers,
        key=lambda provider: CRYPTO_PROVIDER_ORDER.index(provider.name)
        if provider.name in CRYPTO_PROVIDER_ORDER
        else len(CRYPTO_PROVIDER_ORDER),
    )
    for provider in ordered:
        frames: dict[str, list[Candle]] = {}
        for timeframe in TIMEFRAMES:
            candles = provider.fetch_ohlcv(symbol, timeframe)
            if not candles:
                break
            frames[timeframe] = candles
        if set(frames) == set(TIMEFRAMES):
            return provider, frames
    raise RuntimeError("DATA_UNAVAILABLE")
