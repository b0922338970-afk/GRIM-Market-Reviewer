from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer.model import Candle
from market_reviewer.persistence import atomic_write_json, load_review_state, persist_review_state
from market_reviewer.pipeline import review_snapshot
from market_reviewer.reviewer import FairValueGap, _eligible_setup_retest, _setup_id, _state_model
from tests.test_opportunity_v42 import REVIEW29, review28_state
from tests.test_reviewer_v415 import frame


FIXTURES = Path(__file__).parent / "fixtures"
REVIEW30 = FIXTURES / "review30" / "market-data-v1.json"
OLD_SETUP_ID = "M5-BULLISH-SETUP_FVG-1787267400"


def replay_29_then_30() -> tuple[dict, dict, dict]:
    with tempfile.TemporaryDirectory() as directory:
        state_path = Path(directory) / "state.json"
        atomic_write_json(state_path, review28_state())
        review29 = review_snapshot(REVIEW29, state_path)
        review30 = review_snapshot(REVIEW30, state_path)
        persisted, _ = load_review_state(state_path)
    return review29, review30, persisted


def setup(lower: float = 102, upper: float = 106, formed_at: int = 60, status: str = "FRESH") -> FairValueGap:
    return FairValueGap(upper, lower, (lower + upper) / 2, "BULLISH", "M5", formed_at, status, 0, 2, "SETUP_FVG", f"M5:{formed_at}")


class ReviewerV416Tests(unittest.TestCase):
    def test_active_setup_locked_after_setup_created_and_retest_pending(self) -> None:
        review29, review30, _ = replay_29_then_30()
        self.assertEqual(review29["ETH"]["Active_Setup_ID"], OLD_SETUP_ID)
        self.assertEqual(review30["ETH"]["Active_Setup_ID"], OLD_SETUP_ID)
        self.assertIn("2315.85-2317.36", review30["ETH"]["Setup_FVG"])

    def test_later_fvg_does_not_replace_active_setup_or_confirm_old_retest(self) -> None:
        _, review30, _ = replay_29_then_30()
        eth = review30["ETH"]
        self.assertNotIn("2416.30-2416.46", eth["Setup_FVG"])
        self.assertEqual(eth["Eligible_Retest_Confirmed"], "NO")
        self.assertEqual(eth["Eligible_Retest_Timestamp"], "NONE")
        self.assertEqual(eth["State"], "WATCH")

    def test_eligible_retest_setup_id_must_equal_active_setup_id(self) -> None:
        active = setup()
        candles = [Candle(0, 100, 101, 99, 100, 1), Candle(60, 107, 109, 106, 108, 1), Candle(90, 108, 109, 105, 106, 1)]
        evidence = _eligible_setup_retest({"M5": frame(candles, 90)}, active)
        self.assertTrue(evidence.confirmed)
        self.assertEqual(evidence.setup_id, _setup_id(active))
        self.assertNotEqual(evidence.setup_id, "M5-BULLISH-SETUP_FVG-999")

    def test_same_active_setup_legal_later_retest_can_confirm(self) -> None:
        active = setup()
        candles = [Candle(0, 100, 101, 99, 100, 1), Candle(60, 107, 109, 106, 108, 1), Candle(90, 108, 109, 105, 106, 1)]
        evidence = _eligible_setup_retest({"M5": frame(candles, 90)}, active)
        self.assertTrue(evidence.confirmed)
        state, missing, _ = _state_model("BULLISH", "PULLBACK", "REACCELERATION", None, "RETEST_PENDING", "NONE", evidence.confirmed)
        self.assertEqual(state, "ARMED")
        self.assertNotIn("eligible setup retest", missing)

    def test_same_candle_setup_retest_still_rejected(self) -> None:
        active = setup(102, 106, 60)
        candles = [Candle(0, 100, 101, 99, 100, 1), Candle(60, 107, 109, 106, 108, 1)]
        evidence = _eligible_setup_retest({"M5": frame(candles, 60)}, active)
        self.assertFalse(evidence.confirmed)

    def test_invalidated_active_setup_cannot_arm(self) -> None:
        active = setup(102, 106, 60)
        candles = [Candle(0, 100, 101, 99, 100, 1), Candle(60, 107, 109, 106, 108, 1), Candle(90, 104, 105, 101, 101, 1)]
        evidence = _eligible_setup_retest({"M5": frame(candles, 90)}, active)
        self.assertFalse(evidence.confirmed)

    def test_candidate_setup_can_persist_without_production_impact(self) -> None:
        review = copy.deepcopy(replay_29_then_30()[1]["ETH"])
        review["Candidate_Setup_FVG"] = "BULLISH SETUP_FVG M5 2416.30-2416.46 @ 1787333700; status=FRESH"
        review["Candidate_Setup_ID"] = "M5-BULLISH-SETUP_FVG-1787333700"
        previous = {"ETH": review28_state()["symbols"]["ETH"] | {"active_setup_poi": review28_state()["symbols"]["ETH"]["setup_poi"]}}
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            persist_review_state(state_path, {"ETH": review}, previous)
            persisted, _ = load_review_state(state_path)
        self.assertEqual(persisted["ETH"]["active_setup_id"], OLD_SETUP_ID)
        self.assertEqual(persisted["ETH"]["candidate_setup_poi"]["setup_id"], "M5-BULLISH-SETUP_FVG-1787333700")
        self.assertFalse(persisted["ETH"]["eligible_retest_confirmed"])

    def test_fresh_reload_preserves_active_setup_id(self) -> None:
        _, _, persisted = replay_29_then_30()
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, {"persistence_version": 2, "state_schema": "review-state.v2", "symbols": persisted})
            reloaded, _ = load_review_state(state_path)
        self.assertEqual(reloaded["ETH"]["active_setup_id"], OLD_SETUP_ID)
        self.assertIn("2315.85", str(reloaded["ETH"]["active_setup_poi"]))

    def test_replay_preserves_setup_identity_across_long_gap(self) -> None:
        _, review30, persisted = replay_29_then_30()
        self.assertEqual(review30["ETH"]["Active_Setup_ID"], OLD_SETUP_ID)
        self.assertEqual(persisted["ETH"]["active_setup_id"], OLD_SETUP_ID)
        self.assertEqual(persisted["ETH"]["sequence_state"], "RETEST_PENDING")

    def test_review30_eth_does_not_arm_from_2416_setup(self) -> None:
        _, review30, _ = replay_29_then_30()
        eth = review30["ETH"]
        self.assertEqual(eth["State"], "WATCH")
        self.assertEqual(eth["Eligible_Retest_Confirmed"], "NO")
        self.assertIn("eligible setup retest", eth["Missing_Evidence"])

    def test_review30_eth_original_setup_has_no_retest(self) -> None:
        _, review30, _ = replay_29_then_30()
        eth = review30["ETH"]
        self.assertEqual(eth["Setup_FVG"], "BULLISH SETUP_FVG M5 2315.85-2317.36 @ 1787267400; status=FRESH")
        self.assertEqual(eth["Eligible_Retest_Setup_ID"], "NONE")

    def test_review30_btc_lifecycle_remains_unchanged(self) -> None:
        _, review30, persisted = replay_29_then_30()
        btc = review30["BTC"]
        self.assertEqual(btc["Sequence_ID"], "BTC-seq-0007")
        self.assertEqual(btc["Sequence_State"], "SEEKING_LIQUIDITY")
        self.assertEqual(btc["State"], "WAIT")
        self.assertIn("76202.06", btc["Active_Tactical_Draw"])
        retired_ids = [item["liquidity_id"] for item in persisted["BTC"]["retired_liquidity_instances"]]
        self.assertIn("H1-SSL-1787263200", retired_ids)


if __name__ == "__main__":
    unittest.main()