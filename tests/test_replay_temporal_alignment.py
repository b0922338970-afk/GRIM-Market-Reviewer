from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer.external import build_symbol_generation
from market_reviewer.model import Candle, MarketDataFrame, TIMEFRAME_SECONDS, TIMEFRAMES
from market_reviewer.persistence import atomic_write_json, load_review_state
from market_reviewer.pipeline import (
    _frame_at_checkpoint,
    _m5_checkpoints,
    candle_effective_close_timestamp,
    is_candle_available_at_checkpoint,
    review_snapshot,
)


class FakeProvider:
    name = "coinbase"


def candle(timestamp: int, price: float = 100.0) -> Candle:
    return Candle(timestamp, price, price + 2, price - 2, price + 1, 10)


def frame(timeframe: str, timestamps: list[int], latest_closed: int | None = None) -> MarketDataFrame:
    latest_closed = timestamps[-1] if latest_closed is None else latest_closed
    return MarketDataFrame(
        symbol="BTC",
        timeframe=timeframe,
        source="external",
        provider="coinbase",
        market_type="spot",
        timezone="UTC",
        dataset_id=f"BTC-{timeframe}-test",
        generation_id="generation-test",
        generated_at="2026-08-25T00:00:00+00:00",
        source_environment="test",
        completeness_status="DATA_READY",
        fetch_timestamp=timestamps[-1] + TIMEFRAME_SECONDS[timeframe],
        latest_candle_timestamp=timestamps[-1],
        latest_closed_candle_timestamp=latest_closed,
        current_open_candle_timestamp=None if latest_closed == timestamps[-1] else timestamps[-1],
        candles=[candle(ts, 100 + index) for index, ts in enumerate(timestamps)],
        status="DATA_READY",
        warnings=[],
    )


def trending_candles(timeframe: str, start: int, count: int = 70) -> list[Candle]:
    duration = TIMEFRAME_SECONDS[timeframe]
    return [candle(start + index * duration, 100 + index) for index in range(count)]


def previous_state(previous_timestamp: int) -> dict:
    return {
        "persistence_version": 2,
        "state_schema": "review-state.v2",
        "symbols": {
            "BTC": {
                "persistence_version": 2,
                "state_schema": "review-state.v2",
                "symbol": "BTC",
                "previous_bias": "BULLISH",
                "previous_regime": "TREND_PULLBACK",
                "current_phase": "PULLBACK",
                "previous_phase": "CONTINUATION",
                "previous_state": "WAIT",
                "previous_score": "UNCALIBRATED",
                "previous_review_timestamp": previous_timestamp,
                "eligible_retest_confirmed": False,
                "eligible_retest_setup_id": "NONE",
                "active_setup_id": "NONE",
                "active_setup_poi": None,
                "candidate_setup_poi": None,
                "active_tactical_draw": None,
                "candidate_tactical_draw": None,
                "sequence_id": "BTC-seq-test",
                "sequence_state": "EXPIRED_NO_TRIGGER",
                "sequence_started_at": previous_timestamp - 3600,
                "last_sequence_transition": {
                    "previous_state": "SEEKING_LIQUIDITY",
                    "new_state": "EXPIRED_NO_TRIGGER",
                    "timestamp": previous_timestamp - 3600,
                    "evidence": "test previous state",
                },
                "sequence_transition_history": [],
                "target_changed": "NO",
                "target_change_reason": "NONE",
                "target_transition_history": [],
                "retired_liquidity_instances": [],
            }
        },
    }


class ReplayTemporalAlignmentTests(unittest.TestCase):
    def test_d1_becomes_visible_exactly_at_close_boundary(self) -> None:
        d1_open = 1_787_529_600
        previous_d1_open = d1_open - TIMEFRAME_SECONDS["D1"]
        d1 = frame("D1", [previous_d1_open, d1_open, d1_open + TIMEFRAME_SECONDS["D1"]], latest_closed=d1_open)
        visible = _frame_at_checkpoint(d1, d1_open + TIMEFRAME_SECONDS["D1"])
        self.assertEqual(visible.latest_closed_candle_timestamp, d1_open)

    def test_d1_not_visible_one_m5_checkpoint_early(self) -> None:
        d1_open = 1_787_529_600
        previous_d1_open = d1_open - TIMEFRAME_SECONDS["D1"]
        d1 = frame("D1", [previous_d1_open, d1_open, d1_open + TIMEFRAME_SECONDS["D1"]], latest_closed=d1_open)
        early = _frame_at_checkpoint(d1, d1_open + TIMEFRAME_SECONDS["D1"] - TIMEFRAME_SECONDS["M5"])
        self.assertEqual(early.latest_closed_candle_timestamp, previous_d1_open)

    def test_h4_not_visible_early(self) -> None:
        self.assert_timeframe_not_visible_early("H4")

    def test_h1_not_visible_early(self) -> None:
        self.assert_timeframe_not_visible_early("H1")

    def test_m15_not_visible_early(self) -> None:
        self.assert_timeframe_not_visible_early("M15")

    def assert_timeframe_not_visible_early(self, timeframe: str) -> None:
        duration = TIMEFRAME_SECONDS[timeframe]
        current_open = 1_700_000_000
        previous_open = current_open - duration
        test_frame = frame(timeframe, [previous_open, current_open, current_open + duration], latest_closed=current_open)
        early = _frame_at_checkpoint(test_frame, current_open + duration - 1)
        visible = _frame_at_checkpoint(test_frame, current_open + duration)
        self.assertEqual(early.latest_closed_candle_timestamp, previous_open)
        self.assertEqual(visible.latest_closed_candle_timestamp, current_open)

    def test_m5_newly_closed_candle_available_at_replay_event(self) -> None:
        m5_open = 1_700_000_300
        test_frame = frame("M5", [1_700_000_000, m5_open, m5_open + 300], latest_closed=m5_open)
        replay_event = candle_effective_close_timestamp(m5_open, "M5")
        sliced = _frame_at_checkpoint(test_frame, replay_event)
        self.assertEqual(sliced.latest_closed_candle_timestamp, m5_open)

    def test_m5_open_candle_not_closed_before_effective_close(self) -> None:
        m5_open = 1_700_000_300
        test_frame = frame("M5", [1_700_000_000, m5_open], latest_closed=m5_open)
        self.assertFalse(is_candle_available_at_checkpoint(candle(m5_open), "M5", m5_open + 299))
        self.assertTrue(is_candle_available_at_checkpoint(candle(m5_open), "M5", m5_open + 300))

    def test_no_duplicate_m5_replay_after_previous_open_timestamp(self) -> None:
        previous = 1_700_000_000
        test_frame = frame("M5", [previous, previous + 300, previous + 600], latest_closed=previous + 600)
        self.assertEqual(_m5_checkpoints({"M5": test_frame}, previous), [previous + 600, previous + 900])

    def test_previous_review_timestamp_boundary_unchanged(self) -> None:
        previous = 1_700_000_000
        test_frame = frame("M5", [previous, previous + 300], latest_closed=previous + 300)
        checkpoints = _m5_checkpoints({"M5": test_frame}, previous)
        self.assertNotIn(candle_effective_close_timestamp(previous, "M5"), checkpoints)
        self.assertEqual(checkpoints, [previous + 600])

    def test_production_review_timestamp_remains_open_timestamp_identity(self) -> None:
        base = 1_700_000_000
        raw = {}
        for timeframe in TIMEFRAMES:
            duration = TIMEFRAME_SECONDS[timeframe]
            raw[timeframe] = trending_candles(timeframe, base - 65 * duration)
        fetch_timestamp = max(candles[-1].timestamp + TIMEFRAME_SECONDS[timeframe] for timeframe, candles in raw.items())
        snapshot = {"BTC": build_symbol_generation("BTC", FakeProvider(), raw, fetch_timestamp)}
        with tempfile.TemporaryDirectory() as directory:
            snapshot_path = Path(directory) / "market-data-v1.json"
            state_path = Path(directory) / "state.json"
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            atomic_write_json(state_path, previous_state(raw["M5"][-2].timestamp))
            reviews = review_snapshot(snapshot_path, state_path, enforce_replay_coverage=True)
            persisted, _ = load_review_state(state_path)
        self.assertEqual(int(reviews["BTC"]["Review_Timestamp"]), raw["M5"][-1].timestamp)
        self.assertEqual(persisted["BTC"]["previous_review_timestamp"], raw["M5"][-1].timestamp)

    def test_open_candle_contamination_rejected_at_checkpoint(self) -> None:
        h1_open = 1_700_003_600
        test_frame = frame("H1", [1_700_000_000, h1_open], latest_closed=h1_open)
        sliced = _frame_at_checkpoint(test_frame, h1_open + TIMEFRAME_SECONDS["H1"] - 1)
        self.assertEqual(sliced.latest_closed_candle_timestamp, 1_700_000_000)
        self.assertEqual(sliced.current_open_candle_timestamp, h1_open)

    def test_replay_idempotency_preserved_for_checkpoint_slicing(self) -> None:
        h1_open = 1_700_003_600
        test_frame = frame("H1", [1_700_000_000, h1_open], latest_closed=h1_open)
        first = _frame_at_checkpoint(test_frame, h1_open + TIMEFRAME_SECONDS["H1"])
        second = _frame_at_checkpoint(test_frame, h1_open + TIMEFRAME_SECONDS["H1"])
        self.assertEqual(first.latest_closed_candle_timestamp, second.latest_closed_candle_timestamp)
        self.assertEqual(first.current_open_candle_timestamp, second.current_open_candle_timestamp)


if __name__ == "__main__":
    unittest.main()