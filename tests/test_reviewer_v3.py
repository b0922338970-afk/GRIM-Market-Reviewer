from __future__ import annotations

import unittest

from market_reviewer.model import Candle
from market_reviewer.reviewer import (
    LiquidityLevel,
    POI,
    Structure,
    analyze_structure,
    _current_phase,
    _liquidity_draws,
    _market_regime,
    _poi_summary,
)
from test_reviewer_v2 import frame


def bullish_continuation_candles() -> list[Candle]:
    specs = [
        (10, 12, 11),
        (11, 13, 12),
        (12, 15, 14),
        (11, 13, 12),
        (10, 12, 11),
        (9, 11, 10),
        (8, 10, 9),
        (9, 11, 10),
        (10, 12, 11),
        (12, 17, 16),
        (11, 14, 13),
        (10, 13, 12),
        (11, 14, 13),
        (12, 16, 15),
        (13, 17, 16),
        (14, 19, 18),
    ]
    for i in range(16, 60):
        specs.append((14 + i * 0.1, 19 + i * 0.1, 18 + i * 0.1))
    return [Candle(1_700_000_000 + i * 300, close - 0.2, high, low, close, 10) for i, (low, high, close) in enumerate(specs)]


def bearish_reversal_candles() -> list[Candle]:
    candles = bullish_continuation_candles()
    tail = [
        Candle(candles[-1].timestamp + 300, 18, 18.5, 10, 10.5, 10),
        Candle(candles[-1].timestamp + 600, 10, 11, 6, 7, 10),
        Candle(candles[-1].timestamp + 900, 7, 8, 5, 6, 10),
    ]
    return candles + tail


def structure(state: str, protected_high: float | None = None, protected_low: float | None = None) -> Structure:
    return Structure(
        timeframe="H4",
        state=state,
        last_bos=None,
        last_mss=None,
        protected_high=protected_high,
        protected_low=protected_low,
        structural_invalidation_price=protected_low if state == "BULLISH" else protected_high,
        structural_invalidation_type="test",
        last_swing_high=None,
        last_swing_low=None,
        swings=[],
    )


class ReviewerV3Tests(unittest.TestCase):
    def test_bos_continuation_does_not_create_false_mss(self) -> None:
        result = analyze_structure(frame("H1", bullish_continuation_candles()))
        self.assertEqual(result.state, "BULLISH")
        self.assertIsNotNone(result.last_bos)
        self.assertEqual(result.last_bos.direction, "BULLISH")
        self.assertIsNone(result.last_mss)

    def test_mss_reversal_is_separate_from_bos(self) -> None:
        result = analyze_structure(frame("H1", bearish_reversal_candles()))
        self.assertEqual(result.state, "BEARISH")
        self.assertIsNotNone(result.last_mss)
        self.assertEqual(result.last_mss.direction, "BEARISH")

    def test_protected_swing_sets_structural_invalidation(self) -> None:
        result = analyze_structure(frame("H1", bullish_continuation_candles()))
        self.assertIsNotNone(result.protected_low)
        self.assertIn("break below", result.structural_invalidation_type)

    def test_trend_pullback_classification(self) -> None:
        structures = {
            "D1": structure("BULLISH", protected_low=90),
            "H4": structure("BULLISH", protected_low=100),
            "H1": structure("BEARISH", protected_high=130),
            "M15": structure("BULLISH", protected_low=110),
            "M5": structure("BULLISH", protected_low=115),
        }
        phase = _current_phase("BULLISH", structures, price=120)
        self.assertEqual(phase, "PULLBACK")
        self.assertEqual(_market_regime(phase), "TREND_PULLBACK")

    def test_reversal_candidate_when_h4_protection_breaks(self) -> None:
        structures = {
            "D1": structure("BULLISH", protected_low=90),
            "H4": structure("BULLISH", protected_low=100),
            "H1": structure("BEARISH", protected_high=130),
            "M15": structure("BEARISH", protected_high=125),
            "M5": structure("BEARISH", protected_high=123),
        }
        self.assertEqual(_current_phase("BULLISH", structures, price=99), "REVERSAL_CANDIDATE")

    def test_poi_ranking_prefers_aligned_primary(self) -> None:
        aligned = POI("BULLISH FVG H4 95.00-100.00", "FVG", "BULLISH", "H4", 95, 100, 97.5, 1, "FRESH", {}, 10)
        opposing = POI("BEARISH OB H1 101.00-105.00", "OB", "BEARISH", "H1", 101, 105, 103, 1, "FRESH", {}, 8)
        primary, secondary, conflict = _poi_summary([opposing, aligned], "BULLISH")
        self.assertIn("BULLISH", primary)
        self.assertIn("BEARISH", secondary)
        self.assertEqual(conflict, "NONE")

    def test_poi_conflict_when_opposing_zones_overlap(self) -> None:
        aligned = POI("BULLISH FVG H4 95.00-105.00", "FVG", "BULLISH", "H4", 95, 105, 100, 1, "FRESH", {}, 10)
        opposing = POI("BEARISH OB H1 100.00-110.00", "OB", "BEARISH", "H1", 100, 110, 105, 1, "FRESH", {}, 8)
        _, _, conflict = _poi_summary([aligned, opposing], "BULLISH")
        self.assertIn("POI_CONFLICT", conflict)

    def test_macro_vs_tactical_liquidity_draw(self) -> None:
        liquidity = [
            LiquidityLevel(140, "External Buy-side Liquidity", "D1", 1, "UNSWEPT"),
            LiquidityLevel(95, "Internal Sell-side Liquidity", "H1", 1, "UNSWEPT"),
        ]
        macro, tactical = _liquidity_draws("BULLISH", "PULLBACK", liquidity, price=120)
        self.assertIn("140.00", macro)
        self.assertIn("95.00", tactical)


if __name__ == "__main__":
    unittest.main()
