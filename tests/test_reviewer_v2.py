from __future__ import annotations

import unittest
from pathlib import Path

from market_reviewer.model import Candle, MarketDataFrame, TIMEFRAMES
from market_reviewer.pipeline import review_snapshot
from market_reviewer.reviewer import (
    analyze_structure,
    find_fvgs,
    find_liquidity,
    find_order_blocks,
    review_symbol,
)


def frame(timeframe: str, candles: list[Candle]) -> MarketDataFrame:
    return MarketDataFrame(
        symbol="BTC",
        timeframe=timeframe,
        source="external_fetch",
        provider="coinbase",
        market_type="crypto_spot",
        timezone="UTC",
        dataset_id=f"market-data.v1:BTC:{timeframe}",
        generation_id="test-generation",
        generated_at="2026-08-18T00:00:00+00:00",
        source_environment="test",
        completeness_status="DATA_READY",
        fetch_timestamp=candles[-1].timestamp + 300,
        latest_candle_timestamp=candles[-1].timestamp,
        latest_closed_candle_timestamp=candles[-1].timestamp,
        current_open_candle_timestamp=None,
        candles=candles,
        status="DATA_READY",
        warnings=[],
    )


def structure_candles() -> list[Candle]:
    lows = [10, 11, 12, 9, 13, 14, 15, 12, 16, 17, 18, 14, 19, 20, 21, 17, 22, 23, 24, 20]
    highs = [15, 16, 17, 14, 18, 19, 20, 17, 22, 23, 24, 21, 26, 27, 28, 25, 31, 32, 33, 30]
    candles = []
    for i in range(60):
        pivot = i % len(lows)
        low = lows[pivot] + i * 0.2
        high = highs[pivot] + i * 0.2
        close = high - 0.5 if i > 35 else (high + low) / 2
        candles.append(Candle(1_700_000_000 + i * 300, close - 0.2, high, low, close, 10))
    return candles


class ReviewerV2Tests(unittest.TestCase):
    def test_structure_uses_confirmed_swings_for_bos(self) -> None:
        result = analyze_structure(frame("M15", structure_candles()))
        self.assertTrue(result.swings)
        self.assertIn(result.last_bos, {"BULLISH", "BEARISH", "NONE"})
        self.assertNotEqual(result.state, "RANGE")

    def test_liquidity_levels_are_from_confirmed_swings(self) -> None:
        market = frame("H4", structure_candles())
        structure = analyze_structure(market)
        levels = find_liquidity(market, structure)
        self.assertTrue(any(level.type == "External Buy-side Liquidity" for level in levels))
        self.assertTrue(all(level.status in {"UNSWEPT", "SWEPT", "RECLAIMED"} for level in levels))

    def test_fvg_three_candle_detection(self) -> None:
        candles = [
            Candle(1, 10, 12, 9, 11, 1),
            Candle(2, 11, 13, 10, 12, 1),
            Candle(3, 15, 16, 14, 15, 1),
        ]
        market = frame("M5", candles * 20)
        gaps = find_fvgs(market)
        self.assertTrue(any(gap.direction == "BULLISH" and gap.lower == 12 for gap in gaps))

    def test_order_block_requires_bos_and_displacement(self) -> None:
        market = frame("M5", structure_candles())
        structure = analyze_structure(market)
        blocks = find_order_blocks(market, structure)
        if structure.last_bos == "NONE":
            self.assertEqual(blocks, [])
        else:
            self.assertTrue(all(block.direction == structure.last_bos for block in blocks))

    def test_current_poi_is_not_latest_close(self) -> None:
        frames = {tf: frame(tf, structure_candles()) for tf in TIMEFRAMES}
        review = review_symbol(frames).to_dict()
        latest_close = f"{frames['M5'].closed_candles()[-1].close:.2f}"
        self.assertNotEqual(review["Current_POI"], latest_close)
        self.assertEqual(review["Confidence"], "UNCALIBRATED")

    def test_real_artifact_regression_initializes_without_execution(self) -> None:
        path = Path("tests/fixtures/market-data-v1-run-32176441918-1.json")
        reviews = review_snapshot(path)
        self.assertEqual(set(reviews), {"BTC", "ETH"})
        for review in reviews.values():
            self.assertEqual(review["Thesis_Status"], "THESIS_INITIALIZED")
            self.assertNotEqual(review["State"], "EXECUTION")
            self.assertEqual(review["Confidence"], "UNCALIBRATED")
            self.assertTrue(review["Missing_Evidence"])


if __name__ == "__main__":
    unittest.main()
