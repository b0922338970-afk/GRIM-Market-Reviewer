from __future__ import annotations

import unittest

from market_reviewer.reviewer import (
    LiquidityEvent,
    LiquidityLevel,
    _events_for_active_target,
    _events_for_level,
    _pullback_stage,
    _resolve_tactical_targets,
    _sequence_state,
)


def level(price: float, formed_at: int = 100) -> LiquidityLevel:
    return LiquidityLevel(price, "Internal Sell-side Liquidity", "H1", formed_at, "UNSWEPT")


def event(price: float, event_type: str = "SWEPT", timestamp: int = 200) -> LiquidityEvent:
    return LiquidityEvent(price, "Internal Sell-side Liquidity", "H1", event_type, timestamp, price - 1, 1, "OUTSIDE")


def previous(active_price: float = 1891.81, state: str = "SEEKING_LIQUIDITY") -> dict:
    return {
        "sequence_state": state,
        "active_tactical_draw": {
            "price": active_price,
            "type": "Internal Sell-side Liquidity",
            "timeframe": "H1",
            "formed_at": 100,
        },
    }


class ReviewerV41Tests(unittest.TestCase):
    def test_active_target_persists_while_seeking_liquidity(self) -> None:
        active = level(1891.81)
        candidate = level(1909.66, 300)
        resolved = _resolve_tactical_targets(previous(), [active, candidate], candidate, [], 1910)
        self.assertEqual(resolved[0].price, 1891.81)
        self.assertEqual(resolved[1].price, 1909.66)
        self.assertEqual(resolved[4], "NO")
        self.assertEqual(resolved[5], "ACTIVE_DRAW_LOCKED")

    def test_candidate_target_does_not_replace_active_target(self) -> None:
        active = level(1891.81)
        candidate = level(1909.66, 300)
        candidate_event = event(1909.66, "APPROACHED")
        resolved = _resolve_tactical_targets(previous(), [active, candidate], candidate, [candidate_event], 1910)
        self.assertEqual(resolved[0].price, 1891.81)
        self.assertEqual(resolved[2], "NONE")
        self.assertEqual(resolved[3], "APPROACHED")

    def test_candidate_sweep_does_not_advance_active_sequence(self) -> None:
        active = level(1891.81)
        candidate = level(1909.66, 300)
        candidate_sweep = event(1909.66, "SWEPT")
        resolved_active = _resolve_tactical_targets(previous(), [active, candidate], candidate, [candidate_sweep], 1910)[0]
        active_events = _events_for_level([candidate_sweep], resolved_active)
        pullback_stage = _pullback_stage("PULLBACK", active_events, [], "BULLISH")
        sequence_state, _ = _sequence_state("BULLISH", "PULLBACK", pullback_stage, None, None, None, None)
        self.assertEqual(pullback_stage, "SEEKING_LIQUIDITY")
        self.assertEqual(sequence_state, "SEEKING_LIQUIDITY")

    def test_active_sweep_advances_sequence(self) -> None:
        active = level(1891.81)
        candidate = level(1909.66, 300)
        active_sweep = event(1891.81, "SWEPT")
        resolved_active = _resolve_tactical_targets(previous(), [active, candidate], candidate, [active_sweep], 1910)[0]
        active_events = _events_for_level([active_sweep], resolved_active)
        pullback_stage = _pullback_stage("PULLBACK", active_events, [], "BULLISH")
        sequence_state, _ = _sequence_state("BULLISH", "PULLBACK", pullback_stage, active_sweep, None, None, None)
        self.assertEqual(pullback_stage, "LIQUIDITY_TAKEN")
        self.assertEqual(sequence_state, "LIQUIDITY_SWEPT")

    def test_explicit_target_reselection_event_changes_active_target(self) -> None:
        active = level(1891.81)
        candidate = level(1909.66, 300)
        thesis = previous()
        thesis["target_reselection"] = {
            "previous_target": thesis["active_tactical_draw"],
            "new_target": {
                "price": 1909.66,
                "type": "Internal Sell-side Liquidity",
                "timeframe": "H1",
                "formed_at": 300,
            },
            "timestamp": 400,
            "reason": "TARGET_RESELECTED",
            "structural_priority_evidence": "manual validation event",
        }
        resolved = _resolve_tactical_targets(thesis, [active, candidate], candidate, [], 1910)
        self.assertEqual(resolved[0].price, 1909.66)
        self.assertEqual(resolved[4], "YES")
        self.assertEqual(resolved[5], "TARGET_RESELECTED")

    def test_historical_active_sweep_before_selection_does_not_advance_sequence(self) -> None:
        active = level(1891.81)
        historical_sweep = event(1891.81, "SWEPT", timestamp=200)
        active_events = _events_for_active_target([historical_sweep], active, selected_at=300)
        pullback_stage = _pullback_stage("PULLBACK", active_events, [], "BULLISH")
        sequence_state, _ = _sequence_state("BULLISH", "PULLBACK", pullback_stage, None, None, None, None)
        self.assertEqual(active_events, [])
        self.assertEqual(pullback_stage, "SEEKING_LIQUIDITY")
        self.assertEqual(sequence_state, "SEEKING_LIQUIDITY")

    def test_target_unlocks_after_invalidation(self) -> None:
        active = level(1891.81)
        candidate = level(1909.66, 300)
        resolved = _resolve_tactical_targets(previous(state="INVALIDATED"), [active, candidate], candidate, [], 1910)
        self.assertEqual(resolved[0].price, 1909.66)
        self.assertEqual(resolved[4], "YES")
        self.assertEqual(resolved[5], "THESIS_INVALIDATED")


if __name__ == "__main__":
    unittest.main()
