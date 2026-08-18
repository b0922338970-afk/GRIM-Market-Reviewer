from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from market_reviewer.external import (
    build_symbol_generation,
    has_new_closed_candle,
    latest_closed_index,
    publish_artifact,
)
from market_reviewer.model import Candle, DataUnavailable, TIMEFRAMES, to_market_data_frame
from market_reviewer.providers import choose_complete_provider


class FakeProvider:
    def __init__(self, name: str, missing: set[str] | None = None) -> None:
        self.name = name
        self.missing = missing or set()

    def fetch_ohlcv(self, symbol: str, timeframe: str) -> list[Candle]:
        if timeframe in self.missing:
            return []
        duration = {"D1": 86400, "H4": 14400, "H1": 3600, "M15": 900, "M5": 300}[timeframe]
        base = 1_700_000_000
        return [
            Candle(base + i * duration, 100 + i, 102 + i, 99 + i, 101 + i, 10)
            for i in range(55)
        ]


def closed_fetch_timestamp(raw: dict[str, list[Candle]]) -> int:
    return max(candles[-1].timestamp + {"D1": 86400, "H4": 14400, "H1": 3600, "M15": 900, "M5": 300}[tf] for tf, candles in raw.items())


class ExternalTests(unittest.TestCase):
    def test_provider_fallback_uses_single_complete_provider(self) -> None:
        provider, frames = choose_complete_provider(
            "BTC",
            [FakeProvider("bitunix_perpetual", {"M5"}), FakeProvider("binance")],
        )
        self.assertEqual(provider.name, "binance")
        self.assertEqual(set(frames), set(TIMEFRAMES))

    def test_open_candle_is_not_latest_closed(self) -> None:
        provider = FakeProvider("binance")
        raw = {tf: provider.fetch_ohlcv("BTC", tf) for tf in TIMEFRAMES}
        fetch_timestamp = raw["M5"][-1].timestamp + 1
        generation = build_symbol_generation("BTC", provider, raw, fetch_timestamp)
        self.assertEqual(
            generation["M5"]["current_open_candle_timestamp"],
            raw["M5"][-1].timestamp,
        )
        self.assertEqual(
            generation["M5"]["latest_closed_candle_timestamp"],
            raw["M5"][-2].timestamp,
        )

    def test_publish_validates_before_writing_artifact(self) -> None:
        provider = FakeProvider("binance")
        raw = {tf: provider.fetch_ohlcv("ETH", tf) for tf in TIMEFRAMES}
        generation = {"ETH": build_symbol_generation("ETH", provider, raw, closed_fetch_timestamp(raw))}
        with tempfile.TemporaryDirectory() as directory:
            path = publish_artifact(generation, Path(directory))
            self.assertTrue(path.exists())

    def test_generation_rejects_too_few_closed_candles(self) -> None:
        provider = FakeProvider("binance")
        raw = {tf: provider.fetch_ohlcv("BTC", tf)[:10] for tf in TIMEFRAMES}
        generation = build_symbol_generation("BTC", provider, raw, closed_fetch_timestamp(raw))
        frame = to_market_data_frame(generation["M5"])
        with self.assertRaises(DataUnavailable):
            from market_reviewer.model import validate_market_data_frame

            validate_market_data_frame(frame)

    def test_no_change_gate_detects_unchanged_closed_candles(self) -> None:
        provider = FakeProvider("binance")
        raw = {tf: provider.fetch_ohlcv("BTC", tf) for tf in TIMEFRAMES}
        snapshot = {"BTC": build_symbol_generation("BTC", provider, raw, closed_fetch_timestamp(raw))}
        index = latest_closed_index(snapshot)
        self.assertFalse(has_new_closed_candle(index, index))
        self.assertTrue(has_new_closed_candle(None, index))


if __name__ == "__main__":
    unittest.main()
