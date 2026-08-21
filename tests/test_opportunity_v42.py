from __future__ import annotations

import copy
import os
import tempfile
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer.model import TIMEFRAMES, to_market_data_frame
from market_reviewer.opportunity import (
    CORE_FEATURE_LIMIT,
    FEATURE_REGISTRY,
    FeatureDefinition,
    build_outcome_tracking,
    decision_candidate_features,
    extract_opportunity_snapshot,
    feature_registry_document,
    validate_feature_registry,
)
from market_reviewer.persistence import atomic_write_json, load_review_state
from market_reviewer.pipeline import load_snapshot, review_snapshot


FIXTURES = Path(__file__).parent / "fixtures"
REVIEW29 = FIXTURES / "review29" / "market-data-v1.json"


def review28_state() -> dict:
    return {
        "persistence_version": 2,
        "state_schema": "review-state.v2",
        "symbols": {
            "BTC": {
                "persistence_version": 2,
                "state_schema": "review-state.v2",
                "symbol": "BTC",
                "previous_bias": "BULLISH",
                "previous_regime": "TREND_CONTINUATION",
                "current_phase": "CONTINUATION",
                "previous_phase": "PULLBACK",
                "previous_state": "WAIT",
                "previous_score": "UNCALIBRATED",
                "previous_review_timestamp": 1787309700,
                "eligible_retest_confirmed": False,
                "setup_poi": None,
                "active_tactical_draw": {
                    "price": 72481.19,
                    "type": "Internal Sell-side Liquidity",
                    "timeframe": "H1",
                    "formed_at": 1787263200,
                    "selected_at": 1787286900,
                    "status": "NONE",
                    "liquidity_id": "H1-SSL-1787263200",
                },
                "candidate_tactical_draw": {
                    "price": 72481.19,
                    "type": "Internal Sell-side Liquidity",
                    "timeframe": "H1",
                    "formed_at": 1787263200,
                    "detected_at": 1787263200,
                    "status": "NONE",
                    "liquidity_id": "H1-SSL-1787263200",
                },
                "sequence_id": "BTC-seq-0006",
                "sequence_state": "SEEKING_LIQUIDITY",
                "sequence_started_at": 1787286900,
                "last_sequence_transition": {
                    "previous_state": "NONE",
                    "new_state": "SEEKING_LIQUIDITY",
                    "timestamp": 1787286900,
                    "evidence": "pullback_stage=SEEKING_LIQUIDITY",
                },
                "sequence_transition_history": [
                    {"previous_state": "NONE", "new_state": "SEEKING_LIQUIDITY", "timestamp": 1787248500, "evidence": "pullback_stage=SEEKING_LIQUIDITY"},
                    {"previous_state": "SEEKING_LIQUIDITY", "new_state": "EXPIRED_NO_TRIGGER", "timestamp": 1787253600, "evidence": "expired"},
                    {"previous_state": "NONE", "new_state": "SEEKING_LIQUIDITY", "timestamp": 1787262600, "evidence": "pullback_stage=SEEKING_LIQUIDITY"},
                    {"previous_state": "SEEKING_LIQUIDITY", "new_state": "EXPIRED_NO_TRIGGER", "timestamp": 1787286600, "evidence": "expired"},
                    {"previous_state": "NONE", "new_state": "SEEKING_LIQUIDITY", "timestamp": 1787286900, "evidence": "pullback_stage=SEEKING_LIQUIDITY"},
                ],
                "target_changed": "NO",
                "target_change_reason": "ACTIVE_DRAW_LOCKED",
                "target_transition_history": [],
                "retired_liquidity_instances": [
                    {"liquidity_id": "H1-SSL-1787058000", "price": 63981.0, "type": "Internal Sell-side Liquidity", "timeframe": "H1", "formed_at": 1787058000},
                    {"liquidity_id": "H1-SSL-1787119200", "price": 64112.04, "type": "Internal Sell-side Liquidity", "timeframe": "H1", "formed_at": 1787119200},
                    {"liquidity_id": "H1-SSL-1787194800", "price": 68853.22, "type": "Internal Sell-side Liquidity", "timeframe": "H1", "formed_at": 1787194800},
                    {"liquidity_id": "H1-SSL-1787230800", "price": 71065.0, "type": "Internal Sell-side Liquidity", "timeframe": "H1", "formed_at": 1787230800},
                    {"liquidity_id": "H1-SSL-1787252400", "price": 72303.97, "type": "Internal Sell-side Liquidity", "timeframe": "H1", "formed_at": 1787252400},
                ],
            },
            "ETH": {
                "persistence_version": 2,
                "state_schema": "review-state.v2",
                "symbol": "ETH",
                "previous_bias": "BULLISH",
                "previous_regime": "TREND_PULLBACK",
                "current_phase": "PULLBACK",
                "previous_phase": "CONTINUATION",
                "previous_state": "WATCH",
                "previous_score": "UNCALIBRATED",
                "previous_review_timestamp": 1787309700,
                "eligible_retest_confirmed": False,
                "eligible_retest_timestamp": None,
                "eligible_retest_evidence_id": "NONE",
                "setup_poi": {
                    "direction": "BULLISH",
                    "timeframe": "M5",
                    "lower": 2315.85,
                    "upper": 2317.36,
                    "midpoint": 2316.605,
                    "formed_at": 1787267400,
                    "status": "FRESH",
                    "touch_count": 0,
                    "displacement_strength": 0.0,
                    "setup_type": "SETUP_FVG",
                    "related_displacement_id": "M5:1787267400",
                },
                "active_tactical_draw": {
                    "price": 1909.77,
                    "type": "Internal Sell-side Liquidity",
                    "timeframe": "H1",
                    "formed_at": 1787086800,
                    "selected_at": 1787098500,
                    "status": "RECLAIMED",
                    "liquidity_id": "H1-SSL-1787086800",
                },
                "candidate_tactical_draw": {
                    "price": 2336.33,
                    "type": "Internal Sell-side Liquidity",
                    "timeframe": "H1",
                    "formed_at": 1787281200,
                    "detected_at": 1787281200,
                    "status": "NONE",
                    "liquidity_id": "H1-SSL-1787281200",
                },
                "sequence_id": "ETH-seq-0001",
                "sequence_state": "RETEST_PENDING",
                "sequence_started_at": 1787098500,
                "last_sequence_transition": {
                    "previous_state": "SETUP_FVG_CREATED",
                    "new_state": "RETEST_PENDING",
                    "timestamp": 1787267400,
                    "evidence": "setup FVG awaits valid retest",
                },
                "sequence_transition_history": [
                    {"previous_state": "DISPLACEMENT_CONFIRMED", "new_state": "MSS_CONFIRMED", "timestamp": 1787182800, "evidence": "BULLISH 2254.32 @ 1787182800"},
                    {"previous_state": "MSS_CONFIRMED", "new_state": "SETUP_FVG_CREATED", "timestamp": 1787267400, "evidence": "BULLISH SETUP_FVG M5 2315.85-2317.36 @ 1787267400; status=FRESH"},
                    {"previous_state": "SETUP_FVG_CREATED", "new_state": "RETEST_PENDING", "timestamp": 1787267400, "evidence": "setup FVG awaits valid retest"},
                ],
                "target_changed": "NO",
                "target_change_reason": "ACTIVE_DRAW_LOCKED",
                "target_transition_history": [],
                "retired_liquidity_instances": [],
            },
        },
    }


def review29_reviews() -> tuple[dict, dict, dict]:
    with tempfile.TemporaryDirectory() as directory:
        state_path = Path(directory) / "state.json"
        atomic_write_json(state_path, review28_state())
        reviews = review_snapshot(REVIEW29, state_path)
        persisted, _ = load_review_state(state_path)
    return reviews, persisted, snapshot_frames()


def snapshot_frames() -> dict[str, dict]:
    raw = load_snapshot(REVIEW29)
    return {
        symbol: {timeframe: to_market_data_frame(raw[symbol][timeframe]) for timeframe in TIMEFRAMES}
        for symbol in raw
    }


class OpportunityV42Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.reviews, cls.persisted, cls.frames = review29_reviews()

    def test_same_fixture_same_raw_metrics(self) -> None:
        first = extract_opportunity_snapshot(self.reviews["ETH"], self.frames["ETH"])
        second = extract_opportunity_snapshot(self.reviews["ETH"], self.frames["ETH"])
        self.assertEqual(first["raw_metrics"], second["raw_metrics"])

    def test_same_fixture_same_features(self) -> None:
        first = extract_opportunity_snapshot(self.reviews["BTC"], self.frames["BTC"])
        second = extract_opportunity_snapshot(self.reviews["BTC"], self.frames["BTC"])
        self.assertEqual(first["features"], second["features"])

    def test_available_at_never_after_snapshot(self) -> None:
        snapshot = extract_opportunity_snapshot(self.reviews["ETH"], self.frames["ETH"])
        for feature in snapshot["features"].values():
            self.assertLessEqual(feature["available_at"], snapshot["snapshot_timestamp"])

    def test_future_outcome_never_enters_feature_snapshot(self) -> None:
        snapshot = extract_opportunity_snapshot(self.reviews["ETH"], self.frames["ETH"])
        outcome = build_outcome_tracking(snapshot, "CANDIDATE_POI", 1787310600, 2385.52)
        self.assertNotIn("outcomes", snapshot)
        self.assertEqual(outcome["opportunity_id"], snapshot["opportunity_id"])
        self.assertEqual(outcome["schema_version"], "outcome-tracking.v1")

    def test_raw_null_when_source_unavailable(self) -> None:
        snapshot = extract_opportunity_snapshot(self.reviews["BTC"], self.frames["BTC"])
        self.assertIsNone(snapshot["raw_metrics"]["MOMENTUM"]["volume_ratio_if_available"])
        self.assertIn("MOMENTUM.volume_ratio_if_available", snapshot["availability"])

    def test_feature_registry_cap_enforced(self) -> None:
        extras = tuple(
            FeatureDefinition(f"EXTRA_{index}", f"Extra {index}", "STRUCTURE", "DECISION_FEATURE", "CANDIDATE", 1, "extra", "extra", "latest closed")
            for index in range(CORE_FEATURE_LIMIT)
        )
        with self.assertRaises(ValueError):
            validate_feature_registry(FEATURE_REGISTRY + extras)

    def test_duplicate_conceptual_decision_feature_rejected(self) -> None:
        duplicate = FeatureDefinition("HTF_ALIGNMENT_DUP", "HTF Alignment", "STRUCTURE", "DECISION_FEATURE", "CANDIDATE", 1, "duplicate", "duplicate", "latest closed")
        with self.assertRaises(ValueError):
            validate_feature_registry(FEATURE_REGISTRY + (duplicate,))

    def test_deprecated_feature_excluded_from_decision_candidates(self) -> None:
        deprecated = FeatureDefinition("OLD_SIGNAL", "Old Signal", "STRUCTURE", "DECISION_FEATURE", "DEPRECATED", 1, "old", "old", "latest closed")
        candidates = decision_candidate_features(FEATURE_REGISTRY + (deprecated,))
        self.assertNotIn("OLD_SIGNAL", [item.feature_id for item in candidates])

    def test_btc_review29_production_unchanged(self) -> None:
        before = copy.deepcopy(self.reviews["BTC"])
        extract_opportunity_snapshot(self.reviews["BTC"], self.frames["BTC"])
        self.assertEqual(before, self.reviews["BTC"])
        self.assertEqual(self.reviews["BTC"]["Sequence_State"], "SEEKING_LIQUIDITY")
        self.assertEqual(self.reviews["BTC"]["State"], "WAIT")
        self.assertIn("72481.19", self.reviews["BTC"]["Active_Tactical_Draw"])

    def test_eth_review29_production_unchanged(self) -> None:
        before = copy.deepcopy(self.reviews["ETH"])
        extract_opportunity_snapshot(self.reviews["ETH"], self.frames["ETH"])
        self.assertEqual(before, self.reviews["ETH"])
        self.assertEqual(self.reviews["ETH"]["Sequence_State"], "RETEST_PENDING")

    def test_eth_no_retest_stays_watch(self) -> None:
        eth = self.reviews["ETH"]
        snapshot = extract_opportunity_snapshot(eth, self.frames["ETH"])
        self.assertEqual(eth["State"], "WATCH")
        self.assertEqual(eth["Eligible_Retest_Confirmed"], "NO")
        self.assertEqual(snapshot["truth"]["eligible_retest"], False)
        self.assertEqual(snapshot["raw_metrics"]["FRESHNESS"]["mitigation_count"], 0)

    def test_sequential_replay_result_unaffected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, review28_state())
            before = review_snapshot(REVIEW29, state_path)
            persisted_before, _ = load_review_state(state_path)
            extract_opportunity_snapshot(before["BTC"], self.frames["BTC"])
            persisted_after, _ = load_review_state(state_path)
        self.assertEqual(persisted_before, persisted_after)
        self.assertEqual(before["BTC"]["Transition"], "NO_TRANSITION")
        self.assertEqual(before["ETH"]["Transition"], "NO_TRANSITION")

    def test_feature_extraction_idempotent(self) -> None:
        first = extract_opportunity_snapshot(self.reviews["ETH"], self.frames["ETH"])
        second = extract_opportunity_snapshot(self.reviews["ETH"], self.frames["ETH"])
        self.assertEqual(first, second)

    def test_fresh_reload_does_not_alter_production(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, review28_state())
            review_snapshot(REVIEW29, state_path)
            persisted_before, _ = load_review_state(state_path)
            extract_opportunity_snapshot(self.reviews["ETH"], self.frames["ETH"])
            persisted_after, _ = load_review_state(state_path)
        self.assertEqual(persisted_before, persisted_after)

    def test_outcome_origin_separation(self) -> None:
        snapshot = extract_opportunity_snapshot(self.reviews["ETH"], self.frames["ETH"])
        gate_a = build_outcome_tracking(snapshot, "GATE_A", 1787137200, 2110.0, {"1H": {"MFE": 1.2, "MAE": 0.4}})
        poi = build_outcome_tracking(snapshot, "CANDIDATE_POI", 1787310600, 2385.52, {"1H": {"MFE": None, "MAE": None}})
        self.assertEqual(gate_a["measurement_origin"], "GATE_A")
        self.assertEqual(poi["measurement_origin"], "CANDIDATE_POI")
        self.assertNotEqual(gate_a["forward_windows"], poi["forward_windows"])

    def test_registry_document_counts_first_v42_features(self) -> None:
        registry = feature_registry_document()
        self.assertEqual(len(registry["features"]), 16)
        self.assertLessEqual(len(decision_candidate_features()), 12)


if __name__ == "__main__":
    unittest.main()
