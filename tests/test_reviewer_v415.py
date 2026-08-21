from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer.persistence import atomic_write_json, load_review_state
from market_reviewer.pipeline import review_snapshot
from market_reviewer.reviewer import (
    BreakEvent,
    DisplacementEvent,
    FairValueGap,
    LiquidityEvent,
    POI,
    SequenceTransition,
    _eligible_setup_retest,
    _resolve_sequence_lifecycle,
    _sequence_state,
    _state_model,
)
from market_reviewer.model import Candle, MarketDataFrame


FIXTURES = Path(__file__).parent / "fixtures"
REVIEW27 = FIXTURES / "review27" / "market-data-v1.json"


def frame(candles: list[Candle], latest_closed: int) -> MarketDataFrame:
    return MarketDataFrame(
        symbol="ETH",
        timeframe="M5",
        source="test",
        provider="test",
        market_type="spot",
        timezone="UTC",
        dataset_id="test",
        generation_id="test",
        generated_at="2026-01-01T00:00:00+00:00",
        source_environment="test",
        completeness_status="DATA_READY",
        fetch_timestamp=latest_closed + 300,
        latest_candle_timestamp=candles[-1].timestamp,
        latest_closed_candle_timestamp=latest_closed,
        current_open_candle_timestamp=None,
        candles=candles,
        status="DATA_READY",
        warnings=[],
    )


def review26_state() -> dict:
    return {
        "persistence_version": 2,
        "state_schema": "review-state.v2",
        "symbols": {
            "BTC": {
                "previous_bias": "BULLISH",
                "previous_regime": "TREND_CONTINUATION",
                "current_phase": "CONTINUATION",
                "previous_state": "WAIT",
                "previous_review_timestamp": 1787241600,
                "sequence_id": "BTC-seq-0003",
                "sequence_state": "EXPIRED_NO_TRIGGER",
                "sequence_started_at": 1787223300,
                "active_tactical_draw": None,
                "candidate_tactical_draw": None,
                "last_sequence_transition": {
                    "previous_state": "SEEKING_LIQUIDITY",
                    "new_state": "EXPIRED_NO_TRIGGER",
                    "timestamp": 1787238900,
                    "evidence": "old_active_target=Internal Sell-side Liquidity 68853.22 on H1; phase=CONTINUATION; continuation confirmed without active sweep",
                },
                "target_changed": "NO",
                "target_change_reason": "AWAITING_NEW_PULLBACK",
                "retired_liquidity_instances": [
                    {"liquidity_id": "H1-SSL-1787058000", "price": 63981.0, "type": "Internal Sell-side Liquidity", "timeframe": "H1", "formed_at": 1787058000, "status": "RETIRED_FOR_SEQUENCE_GENESIS", "retired_at": 1787137200, "retired_by_sequence_id": "BTC-seq-0002", "retired_reason": "EXPIRED_NO_TRIGGER"},
                    {"liquidity_id": "H1-SSL-1787119200", "price": 64112.04, "type": "Internal Sell-side Liquidity", "timeframe": "H1", "formed_at": 1787119200, "status": "RETIRED_FOR_SEQUENCE_GENESIS", "retired_at": 1787163600, "retired_by_sequence_id": "BTC-seq-0002", "retired_reason": "EXPIRED_NO_TRIGGER"},
                    {"liquidity_id": "H1-SSL-1787194800", "price": 68853.22, "type": "Internal Sell-side Liquidity", "timeframe": "H1", "formed_at": 1787194800, "status": "RETIRED_FOR_SEQUENCE_GENESIS", "retired_at": 1787238900, "retired_by_sequence_id": "BTC-seq-0003", "retired_reason": "EXPIRED_NO_TRIGGER"},
                ],
                "target_transition_history": [],
                "sequence_transition_history": [],
            },
            "ETH": {
                "previous_bias": "BULLISH",
                "previous_regime": "TREND_CONTINUATION",
                "current_phase": "CONTINUATION",
                "previous_state": "WATCH",
                "previous_review_timestamp": 1787241600,
                "sequence_id": "ETH-seq-0001",
                "sequence_state": "MSS_CONFIRMED",
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
                    "price": 2309.6,
                    "type": "Internal Sell-side Liquidity",
                    "timeframe": "H1",
                    "formed_at": 1787248800,
                    "detected_at": 1787248800,
                    "status": "RECLAIMED",
                },
                "last_sequence_transition": {
                    "previous_state": "DISPLACEMENT_CONFIRMED",
                    "new_state": "MSS_CONFIRMED",
                    "timestamp": 1787182800,
                    "evidence": "BULLISH 2254.32 @ 1787182800",
                },
                "target_changed": "NO",
                "target_change_reason": "ACTIVE_DRAW_LOCKED",
                "retired_liquidity_instances": [],
                "target_transition_history": [],
                "sequence_transition_history": [],
            },
        },
    }


class ReviewerV415Tests(unittest.TestCase):
    def test_retest_pending_without_retest_is_watch(self) -> None:
        state, missing, _ = _state_model("BULLISH", "PULLBACK", "REACCELERATION", None, "RETEST_PENDING", "NONE", False)
        self.assertEqual(state, "WATCH")
        self.assertIn("eligible setup retest", missing)

    def test_retest_pending_with_eligible_retest_is_armed(self) -> None:
        poi = POI("BULLISH FVG M5", "FVG", "BULLISH", "M5", 102, 106, 104, 21, "FRESH", {}, 10, 0.01)
        state, missing, _ = _state_model("BULLISH", "PULLBACK", "REACCELERATION", poi, "RETEST_PENDING", "NONE", True)
        self.assertEqual(state, "ARMED")
        self.assertNotIn("eligible setup retest", missing)

    def test_same_candle_setup_retest_rejected(self) -> None:
        setup = FairValueGap(106, 102, 104, "BULLISH", "M5", 60, "FRESH", 0, 2, "SETUP_FVG")
        candles = [
            Candle(0, 100, 101, 99, 100, 1),
            Candle(30, 104, 108, 103, 107, 1),
            Candle(60, 107, 109, 106, 108, 1),
        ]
        evidence = _eligible_setup_retest({"M5": frame(candles, 60)}, setup)
        self.assertFalse(evidence.confirmed)

    def test_next_closed_candle_retest_accepted(self) -> None:
        setup = FairValueGap(106, 102, 104, "BULLISH", "M5", 60, "FRESH", 0, 2, "SETUP_FVG")
        candles = [
            Candle(0, 100, 101, 99, 100, 1),
            Candle(30, 104, 108, 103, 107, 1),
            Candle(60, 107, 109, 106, 108, 1),
            Candle(90, 108, 109, 105, 106, 1),
        ]
        evidence = _eligible_setup_retest({"M5": frame(candles, 90)}, setup)
        self.assertTrue(evidence.confirmed)
        self.assertEqual(evidence.timestamp, 90)

    def test_transition_history_preserves_setup_created_stage(self) -> None:
        sweep = LiquidityEvent(1909.77, "Internal Sell-side Liquidity", "H1", "SWEPT", 10, 1905, 4, "OUTSIDE")
        displacement = DisplacementEvent("BULLISH", "VALID", 20, "NONE", True, 2, 2, True, True)
        mss = BreakEvent("BULLISH", 2254.32, 30, "MSS")
        setup = FairValueGap(2260.0, 2255.0, 2257.5, "BULLISH", "M5", 35, "FRESH", 0, 4, "SETUP_FVG", "M5:35")
        computed_state, computed_transitions = _sequence_state("BULLISH", "PULLBACK", "REACCELERATION", sweep, displacement, mss, setup)
        state, transitions, _, _ = _resolve_sequence_lifecycle(
            {"persistence_version": 2, "state_schema": "review-state.v2", "sequence_state": "MSS_CONFIRMED"},
            "ETH-seq-0001",
            computed_state,
            computed_transitions,
            False,
        )
        self.assertEqual(state, "RETEST_PENDING")
        self.assertEqual([item.new_state for item in transitions], ["SETUP_FVG_CREATED", "RETEST_PENDING"])

    def test_long_gap_review27_native_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, review26_state())
            reviews = review_snapshot(REVIEW27, state_path)
            persisted, _ = load_review_state(state_path)
        btc = reviews["BTC"]
        eth = reviews["ETH"]
        self.assertEqual(btc["Sequence_ID"], "BTC-seq-0005")
        self.assertEqual(btc["Sequence_State"], "SEEKING_LIQUIDITY")
        self.assertIn("72303.97", btc["Active_Tactical_Draw"])
        self.assertEqual(eth["Sequence_State"], "RETEST_PENDING")
        self.assertEqual(eth["State"], "WATCH")
        self.assertEqual(eth["Eligible_Retest_Confirmed"], "NO")
        self.assertIn("2315.85-2317.36", eth["Setup_FVG"])
        eth_history = persisted["ETH"]["sequence_transition_history"]
        self.assertIn("SETUP_FVG_CREATED", [item["new_state"] for item in eth_history])
        self.assertIn("RETEST_PENDING", [item["new_state"] for item in eth_history])
        self.assertFalse(persisted["ETH"]["eligible_retest_confirmed"])
        btc_retired = persisted["BTC"]["retired_liquidity_instances"]
        self.assertIn("H1-SSL-1787230800", [item["liquidity_id"] for item in btc_retired])

    def test_review27_replay_idempotency(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, review26_state())
            first = review_snapshot(REVIEW27, state_path)
            persisted_first, _ = load_review_state(state_path)
            atomic_write_json(state_path, review26_state())
            second = review_snapshot(REVIEW27, state_path)
            persisted_second, _ = load_review_state(state_path)
        self.assertEqual(first["BTC"]["Sequence_ID"], second["BTC"]["Sequence_ID"])
        self.assertEqual(first["ETH"]["Sequence_State"], second["ETH"]["Sequence_State"])
        self.assertEqual(persisted_first["BTC"]["target_transition_history"], persisted_second["BTC"]["target_transition_history"])
        self.assertEqual(persisted_first["ETH"]["sequence_transition_history"], persisted_second["ETH"]["sequence_transition_history"])


if __name__ == "__main__":
    unittest.main()