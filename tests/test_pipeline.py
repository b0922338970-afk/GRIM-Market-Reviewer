from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from market_reviewer.external import build_symbol_generation
from market_reviewer.model import Candle, TIMEFRAMES, to_market_data_frame, validate_generation
from market_reviewer.pipeline import review_snapshot
from market_reviewer.reviewer import review_symbol


class FakeProvider:
    name = "binance"


def candles_for(timeframe: str, bearish: bool = False) -> list[Candle]:
    duration = {"D1": 86400, "H4": 14400, "H1": 3600, "M15": 900, "M5": 300}[timeframe]
    base = 1_700_000_000
    candles = []
    for i in range(60):
        price = 200 - i if bearish else 100 + i
        candles.append(Candle(base + i * duration, price, price + 2, price - 1, price + 1, 10))
    return candles


def closed_fetch_timestamp(raw: dict[str, list[Candle]]) -> int:
    durations = {"D1": 86400, "H4": 14400, "H1": 3600, "M15": 900, "M5": 300}
    return max(candles[-1].timestamp + durations[tf] for tf, candles in raw.items())


def snapshot_for(symbol: str = "BTC", bearish: bool = False) -> dict[str, dict[str, dict]]:
    raw = {tf: candles_for(tf, bearish=bearish) for tf in TIMEFRAMES}
    generation = build_symbol_generation(symbol, FakeProvider(), raw, closed_fetch_timestamp(raw))
    return {symbol: generation}


class PipelineTests(unittest.TestCase):
    def test_generation_requires_same_provider(self) -> None:
        snapshot = snapshot_for("BTC")
        frames = {tf: to_market_data_frame(raw) for tf, raw in snapshot["BTC"].items()}
        frames["M5"].provider = "kraken"
        with self.assertRaises(Exception):
            validate_generation(frames)

    def test_review_uses_allowed_non_execution_state(self) -> None:
        snapshot = snapshot_for("BTC")
        frames = {tf: to_market_data_frame(raw) for tf, raw in snapshot["BTC"].items()}
        review = review_symbol(frames)
        self.assertIn(review.State, {"NO_TRADE", "WAIT", "WATCH", "ARMED"})
        self.assertNotEqual(review.State, "EXECUTION")

    def test_first_review_initializes_thesis(self) -> None:
        snapshot = snapshot_for("ETH")
        frames = {tf: to_market_data_frame(raw) for tf, raw in snapshot["ETH"].items()}
        review = review_symbol(frames)
        self.assertEqual(review.Thesis_Status, "THESIS_INITIALIZED")

    def test_pipeline_loads_snapshot_without_fetching(self) -> None:
        snapshot = snapshot_for("BTC")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "market-data-v1.json"
            path.write_text(json.dumps(snapshot), encoding="utf-8")
            reviews = review_snapshot(path)
        self.assertEqual(set(reviews), {"BTC"})
        self.assertEqual(reviews["BTC"]["Preferred_Direction"], "LONG")


if __name__ == "__main__":
    unittest.main()
