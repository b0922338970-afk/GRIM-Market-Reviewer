from __future__ import annotations

import unittest

from market_reviewer.model import Candle
from market_reviewer.reviewer import (
    BreakEvent,
    DisplacementEvent,
    FairValueGap,
    LiquidityEvent,
    LiquidityLevel,
    POI,
    Structure,
    StructureEvent,
    _cluster_pois,
    _contextual_mss,
    _make_pois,
    _sequence_state,
    _state_model,
    find_displacements,
    find_fvgs,
    find_liquidity_events,
)
from test_reviewer_v2 import frame


def simple_structure(events: list[StructureEvent] | None = None) -> Structure:
    return Structure(
        timeframe="M5",
        state="BULLISH",
        last_bos=None,
        last_mss=None,
        protected_high=110,
        protected_low=90,
        structural_invalidation_price=90,
        structural_invalidation_type="break below 90.00",
        last_swing_high=None,
        last_swing_low=None,
        swings=[],
        events=events or [],
    )


def displacement_candles(direction: str = "BULLISH") -> list[Candle]:
    candles = [Candle(i, 100, 101, 99, 100.2, 1) for i in range(1, 25)]
    if direction == "BULLISH":
        candles.extend(
            [
                Candle(25, 100, 101, 99, 100, 1),
                Candle(26, 102, 106, 102, 105.8, 1),
                Candle(27, 105, 106, 104, 105.5, 1),
            ]
        )
    else:
        candles.extend(
            [
                Candle(25, 100, 101, 99, 100, 1),
                Candle(26, 99, 100, 95, 95.2, 1),
                Candle(27, 95, 96, 94, 94.5, 1),
            ]
        )
    return candles


class ReviewerV4Tests(unittest.TestCase):
    def test_sweep_detection_records_penetration(self) -> None:
        market = frame("M5", [Candle(1, 99, 100, 98, 99, 1), Candle(2, 99, 103, 98, 102, 1)])
        level = LiquidityLevel(101, "Internal Buy-side Liquidity", "M5", 1, "UNSWEPT")
        events = find_liquidity_events(market, [level])
        sweep = next(event for event in events if event.event_type == "SWEPT")
        self.assertEqual(sweep.sweep_price, 103)
        self.assertEqual(sweep.penetration, 2)

    def test_false_sweep_rejected_when_price_only_touches(self) -> None:
        market = frame("M5", [Candle(1, 99, 100, 98, 99, 1), Candle(2, 99, 101, 98, 100, 1)])
        level = LiquidityLevel(101, "Internal Buy-side Liquidity", "M5", 1, "UNSWEPT")
        events = find_liquidity_events(market, [level])
        self.assertFalse(any(event.event_type == "SWEPT" for event in events))

    def test_reclaim_detection(self) -> None:
        market = frame("M5", [Candle(1, 99, 100, 98, 99, 1), Candle(2, 99, 103, 98, 102, 1), Candle(3, 102, 102, 99, 100, 1)])
        level = LiquidityLevel(101, "Internal Buy-side Liquidity", "M5", 1, "UNSWEPT")
        events = find_liquidity_events(market, [level])
        self.assertTrue(any(event.event_type == "RECLAIMED" for event in events))

    def test_failed_reclaim(self) -> None:
        market = frame("M5", [Candle(1, 99, 100, 98, 99, 1), Candle(2, 99, 103, 98, 102, 1), Candle(3, 102, 104, 101.5, 103, 1)])
        level = LiquidityLevel(101, "Internal Buy-side Liquidity", "M5", 1, "UNSWEPT")
        events = find_liquidity_events(market, [level])
        self.assertEqual(events[-1].event_type, "FAILED_RECLAIM")

    def test_displacement_strength_valid_or_strong(self) -> None:
        candles = displacement_candles()
        event = StructureEvent("BULLISH", 101, 26, "BOS", "BULLISH", "BULLISH", "test")
        displacements = find_displacements(frame("M5", candles), simple_structure([event]))
        self.assertTrue(any(item.strength in {"VALID", "STRONG"} for item in displacements))

    def test_displacement_can_create_setup_fvg(self) -> None:
        candles = displacement_candles()
        event = StructureEvent("BULLISH", 101, 26, "BOS", "BULLISH", "BULLISH", "test")
        displacements = find_displacements(frame("M5", candles), simple_structure([event]))
        fvgs = find_fvgs(frame("M5", candles), displacements)
        self.assertTrue(any(gap.setup_type == "SETUP_FVG" for gap in fvgs))

    def test_mss_before_sweep_rejected(self) -> None:
        structures = {"M15": simple_structure([StructureEvent("BULLISH", 105, 10, "MSS", "BEARISH", "BULLISH", "old")]), "M5": simple_structure()}
        sweep = LiquidityEvent(100, "Internal Sell-side Liquidity", "M5", "SWEPT", 20, 99, 1, "OUTSIDE")
        displacement = DisplacementEvent("BULLISH", "VALID", 21, "BOS 105", True, 2, 2, True, True)
        self.assertIsNone(_contextual_mss(structures, sweep, displacement, "BULLISH"))

    def test_mss_after_sweep_accepted(self) -> None:
        structures = {"M15": simple_structure([StructureEvent("BULLISH", 105, 22, "MSS", "BEARISH", "BULLISH", "fresh")]), "M5": simple_structure()}
        sweep = LiquidityEvent(100, "Internal Sell-side Liquidity", "M5", "SWEPT", 20, 99, 1, "OUTSIDE")
        displacement = DisplacementEvent("BULLISH", "VALID", 21, "BOS 105", True, 2, 2, True, True)
        self.assertIsNotNone(_contextual_mss(structures, sweep, displacement, "BULLISH"))

    def test_generic_fvg_is_not_setup_fvg(self) -> None:
        gaps = find_fvgs(frame("M5", displacement_candles()), [])
        self.assertTrue(gaps)
        self.assertTrue(all(gap.setup_type == "GENERIC_FVG" for gap in gaps))

    def test_poi_intersection_refinement(self) -> None:
        first = POI("A", "FVG", "BULLISH", "H4", 95, 105, 100, 1, "FRESH", {}, 10, 0.1)
        second = POI("B", "OB", "BULLISH", "H1", 100, 110, 105, 1, "FRESH", {}, 9, 0.1)
        clustered = _cluster_pois([first, second])
        self.assertEqual(clustered[0].lower, 100)
        self.assertEqual(clustered[0].upper, 105)

    def test_poi_too_wide_rejection(self) -> None:
        wide = FairValueGap(150, 100, 125, "BULLISH", "H4", 1, "FRESH", 0, 2, "SETUP_FVG")
        pois = _make_pois(100, "BULLISH", "DISCOUNT", [wide], [], [])
        self.assertEqual(pois, [])

    def test_wait_before_tactical_liquidity(self) -> None:
        sequence_state, _ = _sequence_state("BULLISH", "PULLBACK", "SEEKING_LIQUIDITY", None, None, None, None)
        state, missing, _ = _state_model("BULLISH", "PULLBACK", "SEEKING_LIQUIDITY", None, sequence_state, "NONE")
        self.assertEqual(state, "WAIT")
        self.assertIn("tactical liquidity sweep", missing)

    def test_watch_after_sweep(self) -> None:
        sweep = LiquidityEvent(100, "Internal Sell-side Liquidity", "M5", "SWEPT", 20, 99, 1, "OUTSIDE")
        sequence_state, _ = _sequence_state("BULLISH", "PULLBACK", "LIQUIDITY_TAKEN", sweep, None, None, None)
        state, _, _ = _state_model("BULLISH", "PULLBACK", "LIQUIDITY_TAKEN", None, sequence_state, "NONE")
        self.assertEqual(state, "WATCH")

    def test_armed_only_after_complete_sequence(self) -> None:
        sweep = LiquidityEvent(100, "Internal Sell-side Liquidity", "M5", "SWEPT", 20, 99, 1, "OUTSIDE")
        displacement = DisplacementEvent("BULLISH", "VALID", 21, "BOS 105", True, 2, 2, True, True)
        mss = BreakEvent("BULLISH", 105, 22, "MSS")
        setup = FairValueGap(106, 102, 104, "BULLISH", "M5", 21, "FRESH", 0, 2, "SETUP_FVG")
        poi = POI("BULLISH FVG M5", "FVG", "BULLISH", "M5", 102, 106, 104, 21, "FRESH", {}, 10, 0.01)
        sequence_state, _ = _sequence_state("BULLISH", "PULLBACK", "RECLAIMED", sweep, displacement, mss, setup)
        state, _, _ = _state_model("BULLISH", "PULLBACK", "RECLAIMED", poi, sequence_state, "NONE", True)
        self.assertEqual(state, "ARMED")

    def test_sequence_invalidation(self) -> None:
        sequence_state, transitions = _sequence_state("NONE", "RANGE", "SEEKING_LIQUIDITY", None, None, None, None)
        self.assertEqual(sequence_state, "INVALIDATED")
        self.assertEqual(transitions[-1].new_state, "INVALIDATED")


if __name__ == "__main__":
    unittest.main()
