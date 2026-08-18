"""Provider abstraction for standalone external market data."""

from __future__ import annotations

import json
import time
from typing import Protocol
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .model import Candle, TIMEFRAMES


CRYPTO_PROVIDER_ORDER = ("bitunix_perpetual", "binance", "coinbase", "kraken")
TIMEFRAME_INTERVALS = {
    "bitunix_perpetual": {"D1": "1d", "H4": "4h", "H1": "1h", "M15": "15m", "M5": "5m"},
    "binance": {"D1": "1d", "H4": "4h", "H1": "1h", "M15": "15m", "M5": "5m"},
    "coinbase": {
        "D1": "ONE_DAY",
        "H4": "FOUR_HOUR",
        "H1": "ONE_HOUR",
        "M15": "FIFTEEN_MINUTE",
        "M5": "FIVE_MINUTE",
    },
    "kraken": {"D1": 1440, "H4": 240, "H1": 60, "M15": 15, "M5": 5},
}
TIMEFRAME_SECONDS = {"D1": 86400, "H4": 14400, "H1": 3600, "M15": 900, "M5": 300}


class MarketDataProvider(Protocol):
    name: str

    def fetch_ohlcv(self, symbol: str, timeframe: str) -> list[Candle]:
        """Return candles using provider candle-open timestamps."""


def _get_json(url: str, timeout: float = 15.0) -> object:
    request = Request(url, headers={"User-Agent": "GRIM-Market-Reviewer/1.0"})
    with urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _symbol_usdt(symbol: str) -> str:
    return f"{symbol}USDT"


def _symbol_usd(symbol: str) -> str:
    return f"{symbol}-USD"


class BitunixPerpetualProvider:
    name = "bitunix_perpetual"

    def fetch_ohlcv(self, symbol: str, timeframe: str) -> list[Candle]:
        params = urlencode(
            {
                "symbol": _symbol_usdt(symbol),
                "interval": TIMEFRAME_INTERVALS[self.name][timeframe],
                "limit": 200,
                "type": "LAST_PRICE",
            }
        )
        try:
            payload = _get_json(f"https://fapi.bitunix.com/api/v1/futures/market/kline?{params}")
            rows = payload.get("data", []) if isinstance(payload, dict) else []
            return sorted(
                [
                    Candle(
                        timestamp=int(row["time"]) // 1000,
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row.get("baseVol", row.get("quoteVol", 0))),
                    )
                    for row in rows
                ],
                key=lambda candle: candle.timestamp,
            )
        except (KeyError, TypeError, ValueError, OSError):
            return []


class BinanceProvider:
    name = "binance"

    def fetch_ohlcv(self, symbol: str, timeframe: str) -> list[Candle]:
        params = urlencode(
            {
                "symbol": _symbol_usdt(symbol),
                "interval": TIMEFRAME_INTERVALS[self.name][timeframe],
                "limit": 200,
            }
        )
        try:
            rows = _get_json(f"https://api.binance.com/api/v3/klines?{params}")
            if not isinstance(rows, list):
                return []
            return [
                Candle(
                    timestamp=int(row[0]) // 1000,
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[5]),
                )
                for row in rows
            ]
        except (IndexError, TypeError, ValueError, OSError):
            return []


class CoinbaseProvider:
    name = "coinbase"

    def fetch_ohlcv(self, symbol: str, timeframe: str) -> list[Candle]:
        duration = TIMEFRAME_SECONDS[timeframe]
        end = int(time.time())
        start = end - duration * 200
        params = urlencode(
            {
                "start": str(start),
                "end": str(end),
                "granularity": TIMEFRAME_INTERVALS[self.name][timeframe],
                "limit": 200,
            }
        )
        try:
            payload = _get_json(
                f"https://api.coinbase.com/api/v3/brokerage/market/products/{_symbol_usd(symbol)}/candles?{params}"
            )
            rows = payload.get("candles", []) if isinstance(payload, dict) else []
            return sorted(
                [
                    Candle(
                        timestamp=int(row["start"]),
                        open=float(row["open"]),
                        high=float(row["high"]),
                        low=float(row["low"]),
                        close=float(row["close"]),
                        volume=float(row["volume"]),
                    )
                    for row in rows
                ],
                key=lambda candle: candle.timestamp,
            )
        except (KeyError, TypeError, ValueError, OSError):
            return []


class KrakenProvider:
    name = "kraken"

    def fetch_ohlcv(self, symbol: str, timeframe: str) -> list[Candle]:
        pair = "XBTUSD" if symbol == "BTC" else f"{symbol}USD"
        duration = TIMEFRAME_SECONDS[timeframe]
        since = int(time.time()) - duration * 220
        params = urlencode({"pair": pair, "interval": TIMEFRAME_INTERVALS[self.name][timeframe], "since": since})
        try:
            payload = _get_json(f"https://api.kraken.com/0/public/OHLC?{params}")
            result = payload.get("result", {}) if isinstance(payload, dict) else {}
            key = next((name for name in result if name != "last"), None)
            rows = result.get(key, []) if key else []
            return [
                Candle(
                    timestamp=int(row[0]),
                    open=float(row[1]),
                    high=float(row[2]),
                    low=float(row[3]),
                    close=float(row[4]),
                    volume=float(row[6]),
                )
                for row in rows
            ]
        except (IndexError, TypeError, ValueError, OSError):
            return []


def default_crypto_providers() -> list[MarketDataProvider]:
    return [
        BitunixPerpetualProvider(),
        BinanceProvider(),
        CoinbaseProvider(),
        KrakenProvider(),
    ]


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
