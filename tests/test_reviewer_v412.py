from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from market_reviewer.persistence import PERSISTENCE_VERSION, STATE_SCHEMA, atomic_write_json, load_review_state
from market_reviewer.pipeline import review_snapshot
from market_reviewer.reviewer import (
    BreakEvent,
    LiquidityEvent,
    Structure,
    _events_for_active_target,
    _sequence_id,
    _should_expire_sequence,
    _state_model,
)
from tests.test_reviewer_v41 import level


FIXTURES = Path(__file__).parent / "fixtures"
REVIEW4 = FIXTURES / "review4" / "market-data-v1.json"
REVIEW5 = FIXTURES / "review5" / "market-data-v1.json"


def empty_structure(timeframe: str, bos: BreakEvent | None = None) -> Structure:
    return Structure(
        timeframe=timeframe,
        state="BULLISH",
        last_bos=bos,
        last_mss=None,
        protected_high=None,
        protected_low=None,
        structural_invalidation_price=None,
        structural_invalidation_type="NONE",
        last_swing_high=None,
        last_swing_low=None,
        swings=[],
        events=[],
    )


def structures_with_h1_bos(timestamp: int) -> dict[str, Structure]:
    return {
        "D1": empty_structure("D1"),
        "H4": empty_structure("H4"),
        "H1": empty_structure("H1", BreakEvent("BULLISH", 100, timestamp, "BOS")),
        "M15": empty_structure("M15"),
        "M5": empty_structure("M5", BreakEvent("BULLISH", 100, timestamp + 1, "BOS")),
    }


def v412_state() -> dict:
    return {
        "persistence_version": PERSISTENCE_VERSION,
        "state_schema": STATE_SCHEMA,
        "symbols": {
            "BTC": {
                "previous_bias": "BULLISH",
                "current_phase": "PULLBACK",
                "sequence_id": "BTC-seq-0001",
                "sequence_state": "SEEKING_LIQUIDITY",
                "sequence_started_at": 1787076000,
                "active_tactical_draw": {
                    "price": 63981.0,
                    "type": "Internal Sell-side Liquidity",
                    "timeframe": "H1",
                    "formed_at": 1787058000,
                    "selected_at": 1787076000,
                    "status": "NONE",
                },
                "target_transition_history": [],
            },
            "ETH": {
                "previous_bias": "BULLISH",
                "current_phase": "PULLBACK",
                "sequence_id": "ETH-seq-0001",
                "sequence_state": "SEEKING_LIQUIDITY",
                "sequence_started_at": 1787098500,
                "active_tactical_draw": {
                    "price": 1909.77,
                    "type": "Internal Sell-side Liquidity",
                    "timeframe": "H1",
                    "formed_at": 1787086800,
                    "selected_at": 1787098500,
                    "status": "NONE",
                },
                "target_transition_history": [],
            },
        },
    }


class ReviewerV412Tests(unittest.TestCase):
    def test_missing_sweep_removed_after_liquidity_swept(self) -> None:
        state, missing, _ = _state_model("BULLISH", "PULLBACK", "SEEKING_LIQUIDITY", None, "LIQUIDITY_SWEPT", "NONE")
        self.assertEqual(state, "WATCH")
        self.assertNotIn("tactical liquidity sweep", missing)
        self.assertIn("valid displacement", missing)

    def test_missing_displacement_removed_after_displacement_confirmed(self) -> None:
        state, missing, _ = _state_model("BULLISH", "PULLBACK", "SEEKING_LIQUIDITY", None, "DISPLACEMENT_CONFIRMED", "NONE")
        self.assertEqual(state, "WATCH")
        self.assertNotIn("tactical liquidity sweep", missing)
        self.assertNotIn("valid displacement", missing)
        self.assertIn("contextual MSS/BOS confirmation", missing)

    def test_no_stale_evidence_after_mss_confirmed(self) -> None:
        _, missing, _ = _state_model("BULLISH", "PULLBACK", "SEEKING_LIQUIDITY", None, "MSS_CONFIRMED", "NONE")
        self.assertEqual([item for item in missing if item != "high-quality thesis-aligned POI"], ["SETUP_FVG or valid OB"])

    def test_pullback_seeking_to_continuation_expires(self) -> None:
        previous = {"current_phase": "PULLBACK", "sequence_state": "SEEKING_LIQUIDITY"}
        self.assertTrue(_should_expire_sequence(previous, "CONTINUATION", level(100), None, structures_with_h1_bos(20), "BULLISH", 10))

    def test_single_m5_bounce_does_not_expire(self) -> None:
        previous = {"current_phase": "PULLBACK", "sequence_state": "SEEKING_LIQUIDITY"}
        structures = structures_with_h1_bos(5)
        structures["H1"] = empty_structure("H1")
        self.assertFalse(_should_expire_sequence(previous, "CONTINUATION", level(100), None, structures, "BULLISH", 10))

    def test_active_sweep_prevents_expiry(self) -> None:
        previous = {"current_phase": "PULLBACK", "sequence_state": "SEEKING_LIQUIDITY"}
        sweep = LiquidityEvent(100, "Internal Sell-side Liquidity", "H1", "SWEPT", 20, 99, 1, "OUTSIDE")
        self.assertFalse(_should_expire_sequence(previous, "CONTINUATION", level(100), sweep, structures_with_h1_bos(20), "BULLISH", 10))

    def test_old_sequence_events_cannot_advance_new_sequence(self) -> None:
        old_sweep = LiquidityEvent(100, "Internal Sell-side Liquidity", "H1", "SWEPT", 20, 99, 1, "OUTSIDE")
        self.assertEqual(_events_for_active_target([old_sweep], level(100), selected_at=30), [])

    def test_btc_review5_expires_stale_sequence_and_retains_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, v412_state())
            reviews = review_snapshot(REVIEW5, state_path)
            persisted, _ = load_review_state(state_path)
        btc = reviews["BTC"]
        self.assertEqual(btc["Sequence_State"], "EXPIRED_NO_TRIGGER")
        self.assertEqual(btc["Active_Tactical_Draw"], "NONE")
        self.assertEqual(btc["Target_Change_Reason"], "EXPIRED_NO_TRIGGER")
        self.assertIsNone(persisted["BTC"]["active_tactical_draw"])
        self.assertEqual(persisted["BTC"]["target_transition_history"][-1]["previous_target"]["price"], 63981.0)

    def test_eth_review5_sweep_prevents_expiry_and_missing_sweep_removed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, v412_state())
            reviews = review_snapshot(REVIEW5, state_path)
        eth = reviews["ETH"]
        self.assertEqual(eth["Sequence_State"], "DISPLACEMENT_CONFIRMED")
        self.assertEqual(eth["State"], "WATCH")
        self.assertNotIn("tactical liquidity sweep", eth["Missing_Evidence"])
        self.assertNotIn("valid displacement", eth["Missing_Evidence"])

    def test_new_pullback_creates_new_sequence_id(self) -> None:
        state = v412_state()
        state["symbols"]["BTC"]["sequence_state"] = "EXPIRED_NO_TRIGGER"
        state["symbols"]["BTC"]["active_tactical_draw"] = None
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, state)
            reviews = review_snapshot(REVIEW4, state_path)
        self.assertEqual(reviews["BTC"]["Sequence_ID"], "BTC-seq-0002")
        self.assertNotEqual(reviews["BTC"]["Active_Tactical_Draw"], "NONE")


if __name__ == "__main__":
    unittest.main()
