from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer.persistence import atomic_write_json, load_review_state, persist_review_state
from market_reviewer.pipeline import review_snapshot
from market_reviewer.reviewer import (
    BreakEvent,
    DisplacementEvent,
    FairValueGap,
    LiquidityEvent,
    LiquidityLevel,
    SequenceTransition,
    _liquidity_id,
    _resolve_tactical_targets,
    _resolve_sequence_lifecycle,
    _sequence_state,
    _setup_fvg,
)
from tests.test_reviewer_v412 import REVIEW5, v412_state


FIXTURES = Path(__file__).parent / "fixtures"
REVIEW10 = FIXTURES / "review10" / "market-data-v1.json"


def level(price: float, formed_at: int, level_type: str = "Internal Sell-side Liquidity") -> LiquidityLevel:
    return LiquidityLevel(price, level_type, "H1", formed_at, "UNSWEPT", _liquidity_id({"price": price, "type": level_type, "timeframe": "H1", "formed_at": formed_at}))


def review9_state() -> dict:
    return {
        "persistence_version": 2,
        "state_schema": "review-state.v2",
        "symbols": {
            "BTC": {
                "previous_bias": "BULLISH",
                "previous_regime": "TREND_CONTINUATION",
                "current_phase": "CONTINUATION",
                "previous_state": "WAIT",
                "previous_review_timestamp": 1787164200,
                "sequence_id": "BTC-seq-0002",
                "sequence_state": "EXPIRED_NO_TRIGGER",
                "sequence_started_at": 1787137200,
                "active_tactical_draw": None,
                "candidate_tactical_draw": {
                    "price": 64112.04,
                    "type": "Internal Sell-side Liquidity",
                    "timeframe": "H1",
                    "formed_at": 1787119200,
                    "detected_at": 1787119200,
                    "status": "NONE",
                },
                "target_changed": "NO",
                "target_change_reason": "AWAITING_NEW_PULLBACK",
                "target_transition_history": [
                    {
                        "previous_target": {
                            "price": 64112.04,
                            "type": "Internal Sell-side Liquidity",
                            "timeframe": "H1",
                            "formed_at": 1787119200,
                            "selected_at": 1787137200,
                            "status": "NONE",
                        },
                        "new_target": None,
                        "timestamp": 1787163600,
                        "reason": "EXPIRED_NO_TRIGGER",
                        "sequence_transition": {
                            "previous_state": "SEEKING_LIQUIDITY",
                            "new_state": "EXPIRED_NO_TRIGGER",
                            "timestamp": 1787163600,
                            "evidence": "expired",
                        },
                    }
                ],
            },
            "ETH": {
                "previous_bias": "BULLISH",
                "previous_regime": "TREND_PULLBACK",
                "current_phase": "PULLBACK",
                "previous_state": "WATCH",
                "previous_review_timestamp": 1787164200,
                "sequence_id": "ETH-seq-0001",
                "sequence_state": "DISPLACEMENT_CONFIRMED",
                "sequence_started_at": 1787098500,
                "active_tactical_draw": {
                    "price": 1909.77,
                    "type": "Internal Sell-side Liquidity",
                    "timeframe": "H1",
                    "formed_at": 1787086800,
                    "selected_at": 1787098500,
                    "status": "RECLAIMED",
                },
                "candidate_tactical_draw": {
                    "price": 1904.81,
                    "type": "Internal Sell-side Liquidity",
                    "timeframe": "H1",
                    "formed_at": 1787119200,
                    "detected_at": 1787119200,
                    "status": "NONE",
                },
                "last_sequence_transition": {
                    "previous_state": "LIQUIDITY_SWEPT",
                    "new_state": "DISPLACEMENT_CONFIRMED",
                    "timestamp": 1787137200,
                    "evidence": "BULLISH VALID @ 1787137200",
                },
                "target_changed": "NO",
                "target_change_reason": "ACTIVE_DRAW_LOCKED",
                "target_transition_history": [],
            },
        },
    }


class ReviewerV414Tests(unittest.TestCase):
    def test_expired_target_liquidity_id_is_retired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, v412_state())
            review_snapshot(REVIEW5, state_path)
            persisted, _ = load_review_state(state_path)
        retired = persisted["BTC"]["retired_liquidity_instances"]
        self.assertEqual(retired[-1]["liquidity_id"], "H1-SSL-1787058000")
        self.assertEqual(retired[-1]["status"], "RETIRED_FOR_SEQUENCE_GENESIS")

    def test_same_retired_liquidity_cannot_seed_next_sequence(self) -> None:
        candidate = level(64112.04, 1787119200)
        previous = {
            "persistence_version": 2,
            "state_schema": "review-state.v2",
            "sequence_state": "EXPIRED_NO_TRIGGER",
            "retired_liquidity_instances": [{"liquidity_id": "H1-SSL-1787119200"}],
        }
        resolved = _resolve_tactical_targets(previous, [candidate], candidate, [], 70000)
        self.assertIsNone(resolved[0])
        self.assertEqual(resolved[1].liquidity_id, "H1-SSL-1787119200")

    def test_same_price_new_formed_at_is_new_liquidity_id(self) -> None:
        retired = level(64112.04, 1787119200)
        fresh = level(64112.04, 1787200000)
        previous = {
            "persistence_version": 2,
            "state_schema": "review-state.v2",
            "sequence_state": "EXPIRED_NO_TRIGGER",
            "retired_liquidity_instances": [{"liquidity_id": retired.liquidity_id}],
        }
        resolved = _resolve_tactical_targets(previous, [fresh], fresh, [], 70000)
        self.assertEqual(resolved[0].liquidity_id, "H1-SSL-1787200000")

    def test_retired_liquidity_survives_fresh_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, review9_state())
            first, _ = load_review_state(state_path)
            atomic_write_json(state_path, {"persistence_version": 2, "state_schema": "review-state.v2", "symbols": first})
            second, _ = load_review_state(state_path)
        self.assertEqual(second["BTC"]["retired_liquidity_instances"][-1]["liquidity_id"], "H1-SSL-1787119200")

    def test_pre_mss_fvg_cannot_become_setup_fvg(self) -> None:
        sweep = LiquidityEvent(1909.77, "Internal Sell-side Liquidity", "H1", "SWEPT", 10, 1905, 4, "OUTSIDE")
        displacement = DisplacementEvent("BULLISH", "VALID", 20, "NONE", True, 2, 2, True, True)
        mss = BreakEvent("BULLISH", 2254.32, 30, "MSS")
        pre_mss = FairValueGap(2254.98, 2240.0, 2247.49, "BULLISH", "M15", 20, "FRESH", 0, 4, "SETUP_FVG", "M15:20")
        self.assertIsNone(_setup_fvg([pre_mss], sweep, displacement, mss, "BULLISH"))

    def test_post_mss_fvg_can_become_setup_fvg(self) -> None:
        sweep = LiquidityEvent(1909.77, "Internal Sell-side Liquidity", "H1", "SWEPT", 10, 1905, 4, "OUTSIDE")
        displacement = DisplacementEvent("BULLISH", "VALID", 20, "NONE", True, 2, 2, True, True)
        mss = BreakEvent("BULLISH", 2254.32, 30, "MSS")
        post_mss = FairValueGap(2260.0, 2255.0, 2257.5, "BULLISH", "M15", 35, "FRESH", 0, 4, "SETUP_FVG", "M15:35")
        self.assertEqual(_setup_fvg([post_mss], sweep, displacement, mss, "BULLISH"), post_mss)

    def test_no_retroactive_fvg_promotion(self) -> None:
        sweep = LiquidityEvent(1909.77, "Internal Sell-side Liquidity", "H1", "SWEPT", 10, 1905, 4, "OUTSIDE")
        displacement = DisplacementEvent("BULLISH", "VALID", 20, "NONE", True, 2, 2, True, True)
        mss = BreakEvent("BULLISH", 2254.32, 30, "MSS")
        pre_mss = FairValueGap(2254.98, 2240.0, 2247.49, "BULLISH", "M15", 20, "FRESH", 0, 4, "SETUP_FVG", "M15:20")
        setup = _setup_fvg([pre_mss], sweep, displacement, mss, "BULLISH")
        state, transitions = _sequence_state("BULLISH", "PULLBACK", "REACCELERATION", sweep, displacement, mss, setup)
        self.assertEqual(state, "MSS_CONFIRMED")
        self.assertNotIn("SETUP_FVG_CREATED", [transition.new_state for transition in transitions])

    def test_multi_state_transition_preserves_timestamp_ordering(self) -> None:
        sweep = LiquidityEvent(1909.77, "Internal Sell-side Liquidity", "H1", "SWEPT", 10, 1905, 4, "OUTSIDE")
        displacement = DisplacementEvent("BULLISH", "VALID", 20, "NONE", True, 2, 2, True, True)
        mss = BreakEvent("BULLISH", 2254.32, 30, "MSS")
        setup = FairValueGap(2260.0, 2255.0, 2257.5, "BULLISH", "M15", 35, "FRESH", 0, 4, "SETUP_FVG", "M15:35")
        state, transitions = _sequence_state("BULLISH", "PULLBACK", "REACCELERATION", sweep, displacement, mss, setup)
        self.assertEqual(state, "RETEST_PENDING")
        timestamps = [transition.timestamp for transition in transitions if transition.timestamp]
        self.assertEqual(timestamps, sorted(timestamps))


    def test_new_sequence_transition_timestamp_is_genesis_closed_candle(self) -> None:
        closed_genesis_timestamp = 1787223300
        current_open_timestamp = 1787230800
        state, transitions, transition, reason = _resolve_sequence_lifecycle(
            {"persistence_version": 2, "state_schema": "review-state.v2", "sequence_state": "EXPIRED_NO_TRIGGER"},
            "BTC-seq-0003",
            "SEEKING_LIQUIDITY",
            [],
            True,
            closed_genesis_timestamp,
        )
        self.assertEqual(state, "SEEKING_LIQUIDITY")
        self.assertEqual(transition, "NEW_SEQUENCE BTC-seq-0003")
        self.assertEqual(reason, "pullback_stage=SEEKING_LIQUIDITY")
        self.assertEqual(transitions[-1].previous_state, "NONE")
        self.assertEqual(transitions[-1].new_state, "SEEKING_LIQUIDITY")
        self.assertEqual(transitions[-1].timestamp, closed_genesis_timestamp)
        self.assertNotEqual(transitions[-1].timestamp, 0)
        self.assertNotEqual(transitions[-1].timestamp, current_open_timestamp)

    def test_terminal_to_new_sequence_metadata_survives_fresh_reload(self) -> None:
        closed_genesis_timestamp = 1787223300
        current_open_timestamp = 1787230800
        _, transitions, _, _ = _resolve_sequence_lifecycle(
            {"persistence_version": 2, "state_schema": "review-state.v2", "sequence_state": "EXPIRED_NO_TRIGGER"},
            "BTC-seq-0003",
            "SEEKING_LIQUIDITY",
            [],
            True,
            closed_genesis_timestamp,
        )
        review = {
            "Swing_Bias": "BULLISH",
            "Market_Regime": "TREND_PULLBACK",
            "Current_Phase": "PULLBACK",
            "Macro_Draw_on_Liquidity": "NONE",
            "Structural_Invalidation": {"H1": "NONE"},
            "State": "WAIT",
            "Confidence": "UNCALIBRATED",
            "Review_Timestamp": "1787230500",
            "Active_Tactical_Draw": "Internal Sell-side Liquidity 68853.22 on H1, formed_at=1787194800, distance=0.0423",
            "Candidate_Tactical_Draw": "Internal Sell-side Liquidity 68853.22 on H1, formed_at=1787194800, distance=0.0423",
            "Active_Draw_Selected_At": str(closed_genesis_timestamp),
            "Sequence_Started_At": str(closed_genesis_timestamp),
            "Active_Draw_Status": "NONE",
            "Candidate_Draw_Status": "NONE",
            "Sequence_ID": "BTC-seq-0003",
            "Sequence_State": "SEEKING_LIQUIDITY",
            "Sequence_Transitions": [transition.__dict__ for transition in transitions],
            "Target_Changed": "YES",
            "Target_Change_Reason": "NEW_SEQUENCE_STARTED",
            "Primary_POI": "NONE",
        }
        previous = {
            "BTC": {
                "persistence_version": 2,
                "state_schema": "review-state.v2",
                "sequence_id": "BTC-seq-0002",
                "sequence_state": "EXPIRED_NO_TRIGGER",
                "active_tactical_draw": None,
                "target_transition_history": [],
                "retired_liquidity_instances": [{"liquidity_id": "H1-SSL-1787119200"}],
            }
        }
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            persist_review_state(state_path, {"BTC": review}, previous)
            persisted, _ = load_review_state(state_path)
        transition = persisted["BTC"]["last_sequence_transition"]
        self.assertEqual(persisted["BTC"]["sequence_id"], "BTC-seq-0003")
        self.assertEqual(persisted["BTC"]["sequence_state"], "SEEKING_LIQUIDITY")
        self.assertEqual(persisted["BTC"]["sequence_started_at"], closed_genesis_timestamp)
        self.assertEqual(transition["previous_state"], "NONE")
        self.assertEqual(transition["new_state"], "SEEKING_LIQUIDITY")
        self.assertEqual(transition["timestamp"], closed_genesis_timestamp)
        self.assertNotEqual(transition["timestamp"], 0)
        self.assertNotEqual(transition["timestamp"], current_open_timestamp)
        self.assertEqual(transition["evidence"], "pullback_stage=SEEKING_LIQUIDITY")

    def test_btc_review10_stale_target_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, review9_state())
            reviews = review_snapshot(REVIEW10, state_path)
        btc = reviews["BTC"]
        self.assertEqual(btc["Sequence_State"], "EXPIRED_NO_TRIGGER")
        self.assertEqual(btc["Active_Tactical_Draw"], "NONE")
        self.assertIn("64112.04", btc["Candidate_Tactical_Draw"])
        self.assertEqual(btc["Transition"], "NO_TRANSITION")

    def test_eth_review10_premature_armed_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, review9_state())
            reviews = review_snapshot(REVIEW10, state_path)
        eth = reviews["ETH"]
        self.assertEqual(eth["Sequence_State"], "MSS_CONFIRMED")
        self.assertEqual(eth["State"], "WATCH")
        self.assertIn("1787182800", eth["Contextual_MSS"])
        self.assertEqual(eth["Setup_FVG"], "NONE")
        self.assertIn("SETUP_FVG or valid OB", eth["Missing_Evidence"])


if __name__ == "__main__":
    unittest.main()
