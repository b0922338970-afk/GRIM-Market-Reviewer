from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from market_reviewer.persistence import load_review_state
from market_reviewer.pipeline import review_snapshot
from market_reviewer.reviewer import LiquidityEvent, SequenceTransition, _resolve_sequence_lifecycle
from tests.test_reviewer_v412 import REVIEW5, v412_state


FIXTURES = Path(__file__).parent / "fixtures"
REVIEW4 = FIXTURES / "review4" / "market-data-v1.json"
REVIEW6 = FIXTURES / "review6" / "market-data-v1.json"


def corrected_review5_state(path: Path) -> dict:
    path.write_text(json.dumps(v412_state()), encoding="utf-8")
    review_snapshot(REVIEW5, path)
    return json.loads(path.read_text(encoding="utf-8"))


class ReviewerV413Tests(unittest.TestCase):
    def test_expired_state_survives_fresh_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            corrected_review5_state(state_path)
            reviews = review_snapshot(REVIEW6, state_path)
        btc = reviews["BTC"]
        self.assertEqual(btc["Loaded_Sequence_State"], "EXPIRED_NO_TRIGGER")
        self.assertEqual(btc["Sequence_State"], "EXPIRED_NO_TRIGGER")
        self.assertEqual(btc["Active_Tactical_Draw"], "NONE")
        self.assertEqual(btc["Transition"], "NO_TRANSITION")

    def test_expired_state_cannot_revert_to_seeking(self) -> None:
        state, transitions, transition, _ = _resolve_sequence_lifecycle(
            {"persistence_version": 2, "state_schema": "review-state.v2", "sequence_state": "EXPIRED_NO_TRIGGER"},
            "BTC-seq-0001",
            "SEEKING_LIQUIDITY",
            [SequenceTransition("NONE", "SEEKING_LIQUIDITY", 0, "fresh recompute")],
            False,
        )
        self.assertEqual(state, "EXPIRED_NO_TRIGGER")
        self.assertEqual(transitions, [])
        self.assertEqual(transition, "NO_TRANSITION")

    def test_new_pullback_creates_new_sequence_id_after_expiry(self) -> None:
        state = v412_state()
        state["symbols"]["BTC"]["sequence_state"] = "EXPIRED_NO_TRIGGER"
        state["symbols"]["BTC"]["active_tactical_draw"] = None
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            state_path.write_text(json.dumps(state), encoding="utf-8")
            reviews = review_snapshot(REVIEW4, state_path)
        self.assertEqual(reviews["BTC"]["Sequence_ID"], "BTC-seq-0002")
        self.assertEqual(reviews["BTC"]["Sequence_State"], "MSS_CONFIRMED")
        self.assertNotEqual(reviews["BTC"]["Active_Tactical_Draw"], "NONE")

    def test_displacement_confirmed_survives_fresh_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            corrected_review5_state(state_path)
            reviews = review_snapshot(REVIEW6, state_path)
        eth = reviews["ETH"]
        self.assertEqual(eth["Loaded_Sequence_State"], "RETEST_PENDING")
        self.assertEqual(eth["Sequence_State"], "RETEST_PENDING")
        self.assertEqual(eth["State"], "WATCH")
        self.assertEqual(eth["Eligible_Retest_Confirmed"], "NO")
        self.assertEqual(eth["Transition"], "NO_TRANSITION")

    def test_active_target_survives_fresh_review(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            corrected_review5_state(state_path)
            reviews = review_snapshot(REVIEW6, state_path)
        self.assertIn("1909.77", reviews["ETH"]["Active_Tactical_Draw"])
        self.assertIn("1909.77", reviews["ETH"]["Current_Active_Target"])

    def test_candidate_cannot_replace_active_after_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            corrected_review5_state(state_path)
            reviews = review_snapshot(REVIEW6, state_path)
        eth = reviews["ETH"]
        self.assertIn("1904.81", eth["Candidate_Tactical_Draw"])
        self.assertIn("1909.77", eth["Active_Tactical_Draw"])
        self.assertEqual(eth["Target_Changed"], "NO")

    def test_no_backward_transition(self) -> None:
        state, transitions, transition, _ = _resolve_sequence_lifecycle(
            {"persistence_version": 2, "state_schema": "review-state.v2", "sequence_state": "DISPLACEMENT_CONFIRMED"},
            "ETH-seq-0001",
            "SEEKING_LIQUIDITY",
            [SequenceTransition("NONE", "SEEKING_LIQUIDITY", 0, "fresh recompute")],
            False,
        )
        self.assertEqual(state, "DISPLACEMENT_CONFIRMED")
        self.assertEqual(transitions, [])
        self.assertEqual(transition, "NO_TRANSITION")

    def test_terminal_state_cannot_reopen_same_sequence(self) -> None:
        state, _, transition, _ = _resolve_sequence_lifecycle(
            {"persistence_version": 2, "state_schema": "review-state.v2", "sequence_state": "INVALIDATED"},
            "BTC-seq-0001",
            "RETEST_PENDING",
            [SequenceTransition("SETUP_FVG_CREATED", "RETEST_PENDING", 1, "fresh recompute")],
            False,
        )
        self.assertEqual(state, "INVALIDATED")
        self.assertEqual(transition, "NO_TRANSITION")

    def test_seeking_can_expire(self) -> None:
        state, transitions, transition, _ = _resolve_sequence_lifecycle(
            {"persistence_version": 2, "state_schema": "review-state.v2", "sequence_state": "SEEKING_LIQUIDITY"},
            "BTC-seq-0001",
            "EXPIRED_NO_TRIGGER",
            [SequenceTransition("SEEKING_LIQUIDITY", "EXPIRED_NO_TRIGGER", 10, "expired")],
            False,
        )
        self.assertEqual(state, "EXPIRED_NO_TRIGGER")
        self.assertEqual(transitions[-1].new_state, "EXPIRED_NO_TRIGGER")
        self.assertEqual(transition, "SEEKING_LIQUIDITY -> EXPIRED_NO_TRIGGER")

    def test_active_state_can_move_forward(self) -> None:
        state, transitions, transition, _ = _resolve_sequence_lifecycle(
            {"persistence_version": 2, "state_schema": "review-state.v2", "sequence_state": "LIQUIDITY_SWEPT"},
            "ETH-seq-0001",
            "DISPLACEMENT_CONFIRMED",
            [SequenceTransition("LIQUIDITY_SWEPT", "DISPLACEMENT_CONFIRMED", 20, "valid displacement")],
            False,
        )
        self.assertEqual(state, "DISPLACEMENT_CONFIRMED")
        self.assertEqual(transitions[-1].new_state, "DISPLACEMENT_CONFIRMED")
        self.assertEqual(transition, "LIQUIDITY_SWEPT -> DISPLACEMENT_CONFIRMED")

    def test_fresh_process_lifecycle_restoration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            corrected_review5_state(state_path)
            script = (
                "import json, os; os._walk_symlinks_as_files=False; "
                "from pathlib import Path; "
                "from market_reviewer.pipeline import review_snapshot; "
                f"reviews=review_snapshot(Path({str(REVIEW6.resolve())!r}), Path({str(state_path.resolve())!r})); "
                "print(json.dumps({s: {k: reviews[s][k] for k in ['Sequence_State','Active_Tactical_Draw','State']} for s in ['BTC','ETH']}))"
            )
            result = subprocess.run([sys.executable, "-c", script], cwd=Path.cwd(), text=True, capture_output=True, check=True)
            payload = json.loads(result.stdout)
        self.assertEqual(payload["BTC"]["Sequence_State"], "EXPIRED_NO_TRIGGER")
        self.assertEqual(payload["BTC"]["Active_Tactical_Draw"], "NONE")
        self.assertEqual(payload["ETH"]["Sequence_State"], "RETEST_PENDING")
        self.assertIn("1909.77", payload["ETH"]["Active_Tactical_Draw"])

    def test_no_transition_persist_preserves_last_transition(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            corrected_review5_state(state_path)
            before, _ = load_review_state(state_path)
            review_snapshot(REVIEW6, state_path)
            after, _ = load_review_state(state_path)
        self.assertEqual(after["BTC"]["last_sequence_transition"], before["BTC"]["last_sequence_transition"])
        self.assertEqual(after["ETH"]["last_sequence_transition"], before["ETH"]["last_sequence_transition"])

    def test_btc_review6_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            corrected_review5_state(state_path)
            reviews = review_snapshot(REVIEW6, state_path)
        btc = reviews["BTC"]
        self.assertEqual(btc["Sequence_ID"], "BTC-seq-0001")
        self.assertEqual(btc["Sequence_State"], "EXPIRED_NO_TRIGGER")
        self.assertEqual(btc["Active_Tactical_Draw"], "NONE")
        self.assertIn("64112.04", btc["Candidate_Tactical_Draw"])

    def test_eth_review6_regression(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            corrected_review5_state(state_path)
            reviews = review_snapshot(REVIEW6, state_path)
        eth = reviews["ETH"]
        self.assertEqual(eth["Sequence_ID"], "ETH-seq-0001")
        self.assertEqual(eth["Sequence_State"], "RETEST_PENDING")
        self.assertEqual(eth["State"], "WATCH")
        self.assertEqual(eth["Eligible_Retest_Confirmed"], "NO")
        self.assertIn("1909.77", eth["Active_Tactical_Draw"])
        self.assertIn("1904.81", eth["Candidate_Tactical_Draw"])
        self.assertNotIn("tactical liquidity sweep", eth["Missing_Evidence"])


if __name__ == "__main__":
    unittest.main()
