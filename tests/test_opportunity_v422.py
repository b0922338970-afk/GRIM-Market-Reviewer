from __future__ import annotations

import copy
import os
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer.model import TIMEFRAMES, to_market_data_frame
from market_reviewer.opportunity import extract_opportunity_snapshot
from market_reviewer.pipeline import load_snapshot


REVIEW38_FULL_COVERAGE = Path(r"C:\Users\Administrator\AppData\Local\Temp\tmpinz8ij5d\market-data-v1.json")


def frames(symbol: str = "BTC") -> dict:
    if REVIEW38_FULL_COVERAGE.exists():
        raw = load_snapshot(REVIEW38_FULL_COVERAGE)
        return {timeframe: to_market_data_frame(raw[symbol][timeframe]) for timeframe in TIMEFRAMES}
    raw = load_snapshot(Path(__file__).parent / "fixtures" / "review29" / "market-data-v1.json")
    return {timeframe: to_market_data_frame(raw[symbol][timeframe]) for timeframe in TIMEFRAMES}


def btc38_review() -> dict:
    return {
        "Symbol": "BTC",
        "Sequence_ID": "BTC-seq-0009",
        "Sequence_State": "INVALIDATED",
        "State": "NO_TRADE",
        "Swing_Bias": "BULLISH",
        "Current_Phase": "PULLBACK",
        "Market_Regime": "TREND_PULLBACK",
        "Review_Timestamp": "1787568600",
        "Sequence_Started_At": "1787371200",
        "Active_Tactical_Draw": "Internal Sell-side Liquidity 76670.01 on H1, formed_at=1787544000, distance=0.0123",
        "Setup_FVG": "NONE",
        "Eligible_Retest_Confirmed": "NO",
        "Missing_Evidence": ["rebuild thesis after structural invalidation"],
        "Displacement": "BULLISH VALID @ 1787566500; structure_broken=BOS 77145.07; fvg_created=False; body_ratio=1.723; range_ratio=1.651; close_near_extreme=True; follow_through=False",
        "Contextual_MSS": "NONE",
        "Sequence_Transitions": [
            {"previous_state": "MSS_CONFIRMED", "new_state": "INVALIDATED", "timestamp": 1787471700, "evidence": "ACTIVE_SEQUENCE_INVALIDATED"},
        ],
    }


def eth38_review() -> dict:
    return {
        "Symbol": "ETH",
        "Sequence_ID": "ETH-seq-0001",
        "Sequence_State": "RETEST_PENDING",
        "State": "WATCH",
        "Swing_Bias": "BULLISH",
        "Current_Phase": "CONTINUATION",
        "Market_Regime": "TREND_CONTINUATION",
        "Review_Timestamp": "1787568600",
        "Sequence_Started_At": "1787098500",
        "Active_Tactical_Draw": "Internal Sell-side Liquidity 1909.77 on H1, formed_at=1787086800, distance=0.2251",
        "Setup_FVG": "BULLISH SETUP_FVG M5 2315.85-2317.36 @ 1787267400; status=FRESH",
        "Eligible_Retest_Confirmed": "NO",
        "Eligible_Retest_Setup_ID": "NONE",
        "Missing_Evidence": ["eligible setup retest"],
        "Displacement": "BULLISH VALID @ 1787565600; structure_broken=BOS 2464.58; fvg_created=False; body_ratio=6.529; range_ratio=4.415; close_near_extreme=False; follow_through=True",
        "Contextual_MSS": "NONE",
        "Sequence_Transitions": [],
    }


class OpportunityV422TerminalSemanticsTests(unittest.TestCase):
    def test_invalidated_is_no_active_opportunity(self) -> None:
        snapshot = extract_opportunity_snapshot(btc38_review(), frames("BTC"))
        self.assertEqual(snapshot["opportunity_status"], "NO_ACTIVE_OPPORTUNITY")

    def test_invalidated_adds_invalidated_outcome(self) -> None:
        snapshot = extract_opportunity_snapshot(btc38_review(), frames("BTC"))
        self.assertIn("INVALIDATED", snapshot["outcome_signatures"])

    def test_active_tactical_draw_cannot_override_terminal_status(self) -> None:
        review = btc38_review()
        self.assertNotEqual(review["Active_Tactical_Draw"], "NONE")
        snapshot = extract_opportunity_snapshot(review, frames("BTC"))
        self.assertEqual(snapshot["truth"]["active_tactical_draw"]["price"], 76670.01)
        self.assertEqual(snapshot["opportunity_status"], "NO_ACTIVE_OPPORTUNITY")

    def test_expired_no_trigger_remains_no_active_opportunity(self) -> None:
        review = btc38_review()
        review["Sequence_ID"] = "BTC-seq-0008"
        review["Sequence_State"] = "EXPIRED_NO_TRIGGER"
        review["Active_Tactical_Draw"] = "Internal Sell-side Liquidity 76594.11 on H1, formed_at=1787335200, distance=0.0010"
        review["Sequence_Transitions"] = [{"previous_state": "SEEKING_LIQUIDITY", "new_state": "EXPIRED_NO_TRIGGER", "timestamp": 1787361000, "evidence": "expired"}]
        snapshot = extract_opportunity_snapshot(review, frames("BTC"))
        self.assertEqual(snapshot["opportunity_status"], "NO_ACTIVE_OPPORTUNITY")
        self.assertIn("EXPIRED_NO_TRIGGER", snapshot["outcome_signatures"])

    def test_retest_pending_remains_active_opportunity(self) -> None:
        snapshot = extract_opportunity_snapshot(eth38_review(), frames("ETH"))
        self.assertEqual(snapshot["opportunity_status"], "ACTIVE_OPPORTUNITY")
        self.assertIn("ELIGIBLE_RETEST", snapshot["missing_evidence"])
        self.assertEqual(snapshot["outcome_signatures"], [])

    def test_historical_pre_invalidation_snapshot_immutable(self) -> None:
        historical = btc38_review()
        historical["Sequence_State"] = "MSS_CONFIRMED"
        historical["Review_Timestamp"] = "1787380500"
        before = copy.deepcopy(historical)
        snapshot = extract_opportunity_snapshot(historical, frames("BTC"))
        self.assertEqual(historical, before)
        self.assertNotIn("INVALIDATED", snapshot["outcome_signatures"])

    def test_invalidated_outcome_available_only_at_or_after_terminal_timestamp(self) -> None:
        before = btc38_review()
        before["Review_Timestamp"] = "1787471699"
        at_terminal = btc38_review()
        at_terminal["Review_Timestamp"] = "1787471700"
        self.assertNotIn("INVALIDATED", extract_opportunity_snapshot(before, frames("BTC"))["outcome_signatures"])
        self.assertIn("INVALIDATED", extract_opportunity_snapshot(at_terminal, frames("BTC"))["outcome_signatures"])

    def test_production_review_object_unchanged(self) -> None:
        review = btc38_review()
        before = copy.deepcopy(review)
        extract_opportunity_snapshot(review, frames("BTC"))
        self.assertEqual(review, before)

    def test_feature_formula_outputs_unchanged_by_status_precedence(self) -> None:
        review = eth38_review()
        first = extract_opportunity_snapshot(review, frames("ETH"))
        second = extract_opportunity_snapshot(review, frames("ETH"))
        self.assertEqual(first["raw_metrics"], second["raw_metrics"])
        self.assertEqual(first["features"], second["features"])
        self.assertEqual(len(first["features"]), 16)

    def test_review38_btc_corrected_snapshot_regression(self) -> None:
        snapshot = extract_opportunity_snapshot(btc38_review(), frames("BTC"))
        self.assertEqual(snapshot["sequence_id"], "BTC-seq-0009")
        self.assertEqual(snapshot["sequence_state"], "INVALIDATED")
        self.assertEqual(snapshot["opportunity_status"], "NO_ACTIVE_OPPORTUNITY")
        self.assertIn("INVALIDATED", snapshot["outcome_signatures"])

    def test_review38_eth_regression(self) -> None:
        snapshot = extract_opportunity_snapshot(eth38_review(), frames("ETH"))
        self.assertEqual(snapshot["sequence_state"], "RETEST_PENDING")
        self.assertEqual(snapshot["opportunity_status"], "ACTIVE_OPPORTUNITY")
        self.assertIn("ELIGIBLE_RETEST", snapshot["missing_evidence"])
        self.assertEqual(snapshot["outcome_signatures"], [])

    def test_raw_metric_denominator_stays_36_per_symbol(self) -> None:
        btc = extract_opportunity_snapshot(btc38_review(), frames("BTC"))
        eth = extract_opportunity_snapshot(eth38_review(), frames("ETH"))
        self.assertEqual(sum(len(items) for items in btc["raw_metrics"].values()), 36)
        self.assertEqual(sum(len(items) for items in eth["raw_metrics"].values()), 36)
        self.assertEqual(sum(len(items) for items in btc["raw_metrics"].values()) + sum(len(items) for items in eth["raw_metrics"].values()), 72)


if __name__ == "__main__":
    unittest.main()