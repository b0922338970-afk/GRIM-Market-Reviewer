from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer.opportunity import extract_opportunity_snapshot
from market_reviewer.persistence import atomic_write_json, load_review_state, persist_review_state
from market_reviewer.pipeline import load_snapshot, review_snapshot
from market_reviewer.model import TIMEFRAMES, to_market_data_frame
from market_reviewer.reviewer import SequenceTransition, _resolve_sequence_lifecycle


REVIEW38_FULL_COVERAGE = Path(r"C:\Users\Administrator\AppData\Local\Temp\tmpinz8ij5d\market-data-v1.json")


def previous_state(state: str = "MSS_CONFIRMED") -> dict:
    return {
        "persistence_version": 2,
        "state_schema": "review-state.v2",
        "sequence_state": state,
        "sequence_id": "BTC-seq-0009",
        "previous_review_timestamp": 1787380500,
        "last_sequence_transition": {
            "previous_state": "DISPLACEMENT_CONFIRMED",
            "new_state": state,
            "timestamp": 1787380500,
            "evidence": "BULLISH 77508.96 @ 1787380500",
        },
        "sequence_transition_history": [
            {"previous_state": "NONE", "new_state": "SEEKING_LIQUIDITY", "timestamp": 1787371200, "evidence": "pullback_stage=SEEKING_LIQUIDITY"},
            {"previous_state": "SEEKING_LIQUIDITY", "new_state": "LIQUIDITY_SWEPT", "timestamp": 1787374800, "evidence": "Internal Sell-side Liquidity 77683.37"},
            {"previous_state": "LIQUIDITY_SWEPT", "new_state": "DISPLACEMENT_CONFIRMED", "timestamp": 1787377500, "evidence": "BULLISH VALID"},
            {"previous_state": "DISPLACEMENT_CONFIRMED", "new_state": state, "timestamp": 1787380500, "evidence": "BULLISH 77508.96 @ 1787380500"},
        ],
    }


class ReviewerV419TerminalProvenanceTests(unittest.TestCase):
    def test_invalidated_terminal_transition_gets_nonzero_timestamp(self) -> None:
        transitions = [SequenceTransition("NONE", "INVALIDATED", 1787568600, "ACTIVE_SEQUENCE_INVALIDATED")]
        state, resolved, _, _ = _resolve_sequence_lifecycle(previous_state(), "BTC-seq-0009", "INVALIDATED", transitions, False)
        self.assertEqual(state, "INVALIDATED")
        self.assertEqual(resolved[-1].timestamp, 1787568600)

    def test_invalidated_previous_state_is_actual_prior_state(self) -> None:
        transitions = [SequenceTransition("NONE", "INVALIDATED", 1787568600, "ACTIVE_SEQUENCE_INVALIDATED")]
        _, resolved, transition_text, _ = _resolve_sequence_lifecycle(previous_state(), "BTC-seq-0009", "INVALIDATED", transitions, False)
        self.assertEqual(resolved[-1].previous_state, "MSS_CONFIRMED")
        self.assertEqual(transition_text, "MSS_CONFIRMED -> INVALIDATED")

    def test_terminal_timestamp_is_after_prior_transition(self) -> None:
        transitions = [SequenceTransition("NONE", "INVALIDATED", 1787568600, "ACTIVE_SEQUENCE_INVALIDATED")]
        _, resolved, _, _ = _resolve_sequence_lifecycle(previous_state(), "BTC-seq-0009", "INVALIDATED", transitions, False)
        self.assertGreater(resolved[-1].timestamp, previous_state()["last_sequence_transition"]["timestamp"])

    def test_full_transition_history_is_preserved(self) -> None:
        transitions = [
            SequenceTransition("NONE", "SEEKING_LIQUIDITY", 1787371200, "pullback_stage=SEEKING_LIQUIDITY"),
            SequenceTransition("SEEKING_LIQUIDITY", "LIQUIDITY_SWEPT", 1787374800, "sweep"),
            SequenceTransition("LIQUIDITY_SWEPT", "DISPLACEMENT_CONFIRMED", 1787377500, "displacement"),
            SequenceTransition("DISPLACEMENT_CONFIRMED", "MSS_CONFIRMED", 1787380500, "mss"),
            SequenceTransition("MSS_CONFIRMED", "INVALIDATED", 1787568600, "ACTIVE_SEQUENCE_INVALIDATED"),
        ]
        _, resolved, _, _ = _resolve_sequence_lifecycle(previous_state(""), "BTC-seq-0009", "INVALIDATED", transitions, True, 1787371200)
        self.assertEqual([item.new_state for item in resolved], ["SEEKING_LIQUIDITY", "LIQUIDITY_SWEPT", "DISPLACEMENT_CONFIRMED", "MSS_CONFIRMED", "INVALIDATED"])

    def test_last_sequence_transition_equals_last_history_transition_after_persist(self) -> None:
        review = {
            "Symbol": "BTC",
            "Swing_Bias": "BULLISH",
            "Market_Regime": "TREND_PULLBACK",
            "Current_Phase": "PULLBACK",
            "State": "NO_TRADE",
            "Confidence": "UNCALIBRATED",
            "Review_Timestamp": "1787568600",
            "Active_Tactical_Draw": "NONE",
            "Candidate_Tactical_Draw": "NONE",
            "Setup_FVG": "NONE",
            "Candidate_Setup_FVG": "NONE",
            "Active_Draw_Status": "NONE",
            "Candidate_Draw_Status": "NONE",
            "Sequence_ID": "BTC-seq-0009",
            "Sequence_State": "INVALIDATED",
            "Sequence_Started_At": "1787371200",
            "Target_Changed": "NO",
            "Target_Change_Reason": "ACTIVE_SEQUENCE_INVALIDATED",
            "Primary_POI": "NONE",
            "Structural_Invalidation": {"H1": "break below 1"},
            "Sequence_Transitions": [
                {"previous_state": "MSS_CONFIRMED", "new_state": "INVALIDATED", "timestamp": 1787568600, "evidence": "ACTIVE_SEQUENCE_INVALIDATED"}
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            persist_review_state(path, {"BTC": review}, {"BTC": previous_state()})
            persisted, _ = load_review_state(path)
        self.assertEqual(persisted["BTC"]["last_sequence_transition"], persisted["BTC"]["sequence_transition_history"][-1])

    def test_fresh_reload_preserves_terminal_provenance(self) -> None:
        state = previous_state() | {
            "sequence_state": "INVALIDATED",
            "last_sequence_transition": {"previous_state": "MSS_CONFIRMED", "new_state": "INVALIDATED", "timestamp": 1787568600, "evidence": "ACTIVE_SEQUENCE_INVALIDATED"},
            "sequence_transition_history": previous_state()["sequence_transition_history"] + [{"previous_state": "MSS_CONFIRMED", "new_state": "INVALIDATED", "timestamp": 1787568600, "evidence": "ACTIVE_SEQUENCE_INVALIDATED"}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            atomic_write_json(path, {"persistence_version": 2, "state_schema": "review-state.v2", "symbols": {"BTC": state}})
            reloaded, _ = load_review_state(path)
        self.assertEqual(reloaded["BTC"]["last_sequence_transition"]["timestamp"], 1787568600)
        self.assertEqual(reloaded["BTC"]["last_sequence_transition"]["previous_state"], "MSS_CONFIRMED")

    def test_no_synthetic_none_to_invalidated_zero(self) -> None:
        transitions = [SequenceTransition("NONE", "INVALIDATED", 1787568600, "ACTIVE_SEQUENCE_INVALIDATED")]
        _, resolved, _, _ = _resolve_sequence_lifecycle(previous_state(), "BTC-seq-0009", "INVALIDATED", transitions, False)
        self.assertNotEqual((resolved[-1].previous_state, resolved[-1].timestamp), ("NONE", 0))

    def test_expired_no_trigger_provenance_unchanged(self) -> None:
        transition = SequenceTransition("SEEKING_LIQUIDITY", "EXPIRED_NO_TRIGGER", 1787361000, "expired")
        state, resolved, _, _ = _resolve_sequence_lifecycle(
            {"persistence_version": 2, "state_schema": "review-state.v2", "sequence_state": "SEEKING_LIQUIDITY"},
            "BTC-seq-0008",
            "EXPIRED_NO_TRIGGER",
            [transition],
            False,
        )
        self.assertEqual(state, "EXPIRED_NO_TRIGGER")
        self.assertEqual(resolved[-1].previous_state, "SEEKING_LIQUIDITY")
        self.assertEqual(resolved[-1].timestamp, 1787361000)

    def test_idempotent_terminal_replay_same_timestamp_reason(self) -> None:
        transitions = [SequenceTransition("NONE", "INVALIDATED", 1787568600, "ACTIVE_SEQUENCE_INVALIDATED")]
        first = _resolve_sequence_lifecycle(previous_state(), "BTC-seq-0009", "INVALIDATED", transitions, False)
        second = _resolve_sequence_lifecycle(previous_state(), "BTC-seq-0009", "INVALIDATED", transitions, False)
        self.assertEqual(first, second)

    def test_persistence_block_signal_when_terminal_evidence_missing(self) -> None:
        state, resolved, _, reason = _resolve_sequence_lifecycle(previous_state(), "BTC-seq-0009", "INVALIDATED", [], False)
        self.assertEqual(state, "INVALIDATED")
        self.assertEqual(resolved[-1].evidence, "TERMINAL_TRANSITION_EVIDENCE_MISSING")
        self.assertEqual(reason, "TERMINAL_TRANSITION_EVIDENCE_MISSING")

    def test_v42_production_isolation_unchanged(self) -> None:
        review = {
            "Symbol": "BTC",
            "Sequence_ID": "BTC-seq-0009",
            "Sequence_State": "INVALIDATED",
            "State": "NO_TRADE",
            "Swing_Bias": "BULLISH",
            "Current_Phase": "PULLBACK",
            "Market_Regime": "TREND_PULLBACK",
            "Active_Tactical_Draw": "NONE",
            "Setup_FVG": "NONE",
            "Eligible_Retest_Confirmed": "NO",
            "Missing_Evidence": [],
            "Review_Timestamp": "1787568600",
        }
        artifact = REVIEW38_FULL_COVERAGE
        if not artifact.exists():
            self.skipTest("review38 full coverage artifact unavailable")
        raw = load_snapshot(artifact)
        frames = {tf: to_market_data_frame(raw["BTC"][tf]) for tf in TIMEFRAMES}
        before = json.dumps(review, sort_keys=True)
        extract_opportunity_snapshot(review, frames)
        self.assertEqual(json.dumps(review, sort_keys=True), before)

    def test_long_gap_review38_btc_terminal_transition_regression(self) -> None:
        artifact = REVIEW38_FULL_COVERAGE
        if not artifact.exists():
            self.skipTest("review38 full coverage artifact unavailable")
        production_state = Path("reviews/thesis-baseline.json")
        with tempfile.TemporaryDirectory() as directory:
            temp_state = Path(directory) / "state.json"
            shutil.copy2(production_state, temp_state)
            reviews = review_snapshot(artifact, temp_state, enforce_replay_coverage=True)
            persisted, _ = load_review_state(temp_state)
        btc = persisted["BTC"]
        terminal = btc["last_sequence_transition"]
        self.assertEqual(reviews["BTC"]["Sequence_State"], "INVALIDATED")
        self.assertEqual(terminal["new_state"], "INVALIDATED")
        self.assertNotEqual(terminal["timestamp"], 0)
        self.assertNotEqual(terminal["previous_state"], "NONE")
        self.assertEqual(terminal, btc["sequence_transition_history"][-1])
        self.assertIn("ACTIVE_SEQUENCE_INVALIDATED", terminal["evidence"])


if __name__ == "__main__":
    unittest.main()
