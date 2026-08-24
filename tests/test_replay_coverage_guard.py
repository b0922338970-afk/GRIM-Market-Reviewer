from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer.external import build_symbol_generation
from market_reviewer.model import Candle, TIMEFRAMES, TIMEFRAME_SECONDS, to_market_data_frame
from market_reviewer.opportunity import extract_opportunity_snapshot
from market_reviewer.persistence import atomic_write_json
from market_reviewer.pipeline import replay_coverage_report, review_snapshot


class FakeProvider:
    name = "binance"


PREVIOUS_TIMESTAMP = 1_700_000_000


def previous_state() -> dict:
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
                "previous_review_timestamp": PREVIOUS_TIMESTAMP,
                "eligible_retest_confirmed": False,
                "eligible_retest_setup_id": "NONE",
                "setup_poi": None,
                "active_setup_poi": None,
                "active_tactical_draw": None,
                "candidate_tactical_draw": None,
                "sequence_id": "BTC-seq-test",
                "sequence_state": "EXPIRED_NO_TRIGGER",
                "sequence_started_at": PREVIOUS_TIMESTAMP - 3600,
                "last_sequence_transition": {
                    "previous_state": "SEEKING_LIQUIDITY",
                    "new_state": "EXPIRED_NO_TRIGGER",
                    "timestamp": PREVIOUS_TIMESTAMP - 3600,
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


def candles(timeframe: str, start: int, count: int = 70) -> list[Candle]:
    duration = TIMEFRAME_SECONDS[timeframe]
    return [
        Candle(start + index * duration, 100 + index, 102 + index, 99 + index, 101 + index, 10)
        for index in range(count)
    ]


def snapshot(truncated_timeframe: str | None = None) -> dict:
    raw = {}
    for timeframe in TIMEFRAMES:
        duration = TIMEFRAME_SECONDS[timeframe]
        start = PREVIOUS_TIMESTAMP - 55 * duration
        if timeframe == truncated_timeframe:
            start = PREVIOUS_TIMESTAMP + 2 * duration
        raw[timeframe] = candles(timeframe, start)
    fetch_timestamp = max(frame[-1].timestamp + TIMEFRAME_SECONDS[timeframe] for timeframe, frame in raw.items())
    return {"BTC": build_symbol_generation("BTC", FakeProvider(), raw, fetch_timestamp)}


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


class ReplayCoverageGuardTests(unittest.TestCase):
    def test_replay_coverage_complete_allows_normal_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            snapshot_path = Path(directory) / "market-data-v1.json"
            atomic_write_json(state_path, previous_state())
            write_json(snapshot_path, snapshot())
            reviews = review_snapshot(snapshot_path, state_path, enforce_replay_coverage=True)
        self.assertIn("BTC", reviews)
        self.assertNotIn("REPLAY_DATA_GAP", reviews)

    def test_m5_truncated_before_previous_timestamp_blocks_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            snapshot_path = Path(directory) / "market-data-v1.json"
            atomic_write_json(state_path, previous_state())
            write_json(snapshot_path, snapshot("M5"))
            reviews = review_snapshot(snapshot_path, state_path, enforce_replay_coverage=True)
        gap = reviews["REPLAY_DATA_GAP"]
        self.assertEqual(gap["REPLAY_DATA_GAP"], "YES")
        self.assertEqual(gap["PRODUCTION_NATIVE_SEQUENTIAL_REPLAY"], "BLOCKED")
        self.assertFalse(gap["symbols"]["BTC"]["timeframes"]["M5"]["coverage_complete"])

    def test_m15_truncated_before_previous_timestamp_blocks_replay(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            snapshot_path = Path(directory) / "market-data-v1.json"
            atomic_write_json(state_path, previous_state())
            write_json(snapshot_path, snapshot("M15"))
            reviews = review_snapshot(snapshot_path, state_path, enforce_replay_coverage=True)
        gap = reviews["REPLAY_DATA_GAP"]
        self.assertEqual(gap["REPLAY_COVERAGE_COMPLETE"], "NO")
        self.assertFalse(gap["symbols"]["BTC"]["timeframes"]["M15"]["coverage_complete"])

    def test_no_production_mutation_when_blocked(self) -> None:
        state = previous_state()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            snapshot_path = Path(directory) / "market-data-v1.json"
            atomic_write_json(state_path, state)
            before = json.loads(state_path.read_text(encoding="utf-8"))
            write_json(snapshot_path, snapshot("M5"))
            review_snapshot(snapshot_path, state_path, enforce_replay_coverage=True)
            after = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(before, after)

    def test_no_persistence_when_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            snapshot_path = Path(directory) / "market-data-v1.json"
            atomic_write_json(state_path, previous_state())
            before = state_path.read_text(encoding="utf-8")
            write_json(snapshot_path, snapshot("M15"))
            review_snapshot(snapshot_path, state_path, enforce_replay_coverage=True)
            after = state_path.read_text(encoding="utf-8")
        self.assertEqual(before, after)

    def test_no_official_feature_snapshot_accepted_when_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            snapshot_path = Path(directory) / "market-data-v1.json"
            atomic_write_json(state_path, previous_state())
            write_json(snapshot_path, snapshot("M5"))
            reviews = review_snapshot(snapshot_path, state_path, enforce_replay_coverage=True)
            frames = {tf: to_market_data_frame(raw) for tf, raw in snapshot("M5")["BTC"].items()}
        self.assertNotIn("BTC", reviews)
        self.assertEqual(reviews["REPLAY_DATA_GAP"]["Status"], "REPLAY_DATA_GAP")
        with self.assertRaises(KeyError):
            extract_opportunity_snapshot(reviews["BTC"], frames)

    def test_same_coverage_check_is_deterministic(self) -> None:
        frames = {
            symbol: {tf: to_market_data_frame(raw) for tf, raw in frames.items()}
            for symbol, frames in snapshot("M5").items()
        }
        previous = previous_state()["symbols"]
        first = replay_coverage_report(frames, previous)
        second = replay_coverage_report(copy.deepcopy(frames), copy.deepcopy(previous))
        self.assertEqual(first, second)

    def test_first_required_closed_candle_available_allows_tolerance(self) -> None:
        raw = snapshot()
        first_required = PREVIOUS_TIMESTAMP + TIMEFRAME_SECONDS["M5"]
        raw["BTC"]["M5"] = build_symbol_generation(
            "BTC",
            FakeProvider(),
            {"M5": candles("M5", first_required)},
            first_required + 70 * TIMEFRAME_SECONDS["M5"],
        )["M5"]
        frames = {"BTC": {tf: to_market_data_frame(frame) for tf, frame in raw["BTC"].items()}}
        report = replay_coverage_report(frames, previous_state()["symbols"])
        self.assertEqual(report["REPLAY_DATA_GAP"], "NO")
        self.assertTrue(report["symbols"]["BTC"]["timeframes"]["M5"]["coverage_complete"])


if __name__ == "__main__":
    unittest.main()
