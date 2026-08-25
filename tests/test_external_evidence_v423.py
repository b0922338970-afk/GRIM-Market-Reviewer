from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer.external_evidence import (
    EXTERNAL_EVIDENCE_SCHEMA_VERSION,
    attach_external_evidence_reference,
    build_external_market_evidence,
    classify_cvd_relation,
    classify_funding_relation,
    classify_large_order_relation,
    classify_liquidation_relation,
    classify_oi_price_relation,
    metric,
)
from market_reviewer.model import TIMEFRAMES, to_market_data_frame
from market_reviewer.opportunity import extract_opportunity_snapshot
from market_reviewer.persistence import atomic_write_json, load_review_state
from market_reviewer.pipeline import load_snapshot, review_snapshot
from tests.test_opportunity_v42 import REVIEW29, review28_state


SNAPSHOT_TS = 1_800_000_000


def sample_metric(metric_id: str, value: float, timestamp: int = SNAPSHOT_TS - 60) -> dict:
    return metric(metric_id, value, "fixture", timestamp, timestamp, "5m")


class ExternalEvidenceV423Tests(unittest.TestCase):
    def test_data_unavailable_is_not_negative(self) -> None:
        evidence = build_external_market_evidence("ETH", SNAPSHOT_TS)
        self.assertEqual(evidence["domain_classification"]["CAPITAL_FLOW"]["classification"], "DATA_UNAVAILABLE")
        self.assertNotEqual(evidence["domain_classification"]["CAPITAL_FLOW"]["classification"], "NEGATIVE")

    def test_source_timestamp_after_snapshot_rejected(self) -> None:
        with self.assertRaises(ValueError):
            build_external_market_evidence("ETH", SNAPSHOT_TS, {"oi": sample_metric("oi", 1, SNAPSHOT_TS + 1)})

    def test_available_at_after_snapshot_rejected(self) -> None:
        future = metric("oi", 1, "fixture", SNAPSHOT_TS - 1, SNAPSHOT_TS + 1, "5m")
        with self.assertRaises(ValueError):
            build_external_market_evidence("ETH", SNAPSHOT_TS, {"oi": future})

    def test_stale_source_marked_stale(self) -> None:
        evidence = build_external_market_evidence("ETH", SNAPSHOT_TS, {"oi": sample_metric("oi", 10, SNAPSHOT_TS - 3600)}, stale_after_seconds=300)
        self.assertEqual(evidence["raw_metrics"]["oi"]["availability"], "STALE")

    def test_oi_relation_feature_deterministic(self) -> None:
        self.assertEqual(classify_oi_price_relation(1.2, 4.0), "PRICE_UP_OI_UP")
        first = build_external_market_evidence("BTC", SNAPSHOT_TS, {"oi_change_5m": sample_metric("oi_change_5m", 4.0)}, {"price_change_pct": 1.2})
        second = build_external_market_evidence("BTC", SNAPSHOT_TS, {"oi_change_5m": sample_metric("oi_change_5m", 4.0)}, {"price_change_pct": 1.2})
        self.assertEqual(first["relation_features"]["OI_PRICE_RELATION"], second["relation_features"]["OI_PRICE_RELATION"])

    def test_spot_perp_cvd_divergence_deterministic(self) -> None:
        self.assertEqual(classify_cvd_relation(12.0, -5.0), "SPOT_LED_BUYING")
        evidence = build_external_market_evidence(
            "ETH",
            SNAPSHOT_TS,
            {"spot_cvd_change": sample_metric("spot_cvd_change", 12.0), "perp_cvd_change": sample_metric("perp_cvd_change", -5.0)},
        )
        self.assertEqual(evidence["relation_features"]["SPOT_PERP_CVD_RELATION"]["value"]["relation"], "SPOT_LED_BUYING")

    def test_funding_crowding_relation_deterministic(self) -> None:
        self.assertEqual(classify_funding_relation(1.0, 0.001, 95), "LONG_CROWDING")
        evidence = build_external_market_evidence("ETH", SNAPSHOT_TS, {"funding_percentile": sample_metric("funding_percentile", 95)}, {"price_change_pct": 1.0})
        self.assertEqual(evidence["domain_classification"]["CROWDING"]["classification"], "NEGATIVE")

    def test_liquidation_cluster_relation_deterministic(self) -> None:
        self.assertEqual(classify_liquidation_relation(0.5, None, None, None), "LIQ_CLUSTER_APPROACH")
        evidence = build_external_market_evidence("BTC", SNAPSHOT_TS, {"liq_cluster_distance_pct": sample_metric("liq_cluster_distance_pct", 0.5)})
        self.assertEqual(evidence["relation_features"]["LIQUIDATION_CONTEXT_RELATION"]["value"]["relation"], "LIQ_CLUSTER_APPROACH")

    def test_large_order_absorption_relation_deterministic(self) -> None:
        self.assertEqual(classify_large_order_relation(-0.2, 1_000_000, 100_000, 0.8), "LARGE_BUY_ABSORBED")
        evidence = build_external_market_evidence("BTC", SNAPSHOT_TS, {"large_order_imbalance": sample_metric("large_order_imbalance", 0.8)}, {"price_change_pct": -0.2})
        self.assertEqual(evidence["relation_features"]["LARGE_ORDER_FLOW_RELATION"]["value"]["relation"], "LARGE_BUY_ABSORBED")

    def test_missing_one_provider_does_not_fabricate_evidence(self) -> None:
        evidence = build_external_market_evidence("ETH", SNAPSHOT_TS, {"spot_cvd_change": sample_metric("spot_cvd_change", 7.0)})
        self.assertEqual(evidence["relation_features"]["SPOT_PERP_CVD_RELATION"]["value"]["relation"], "DATA_UNAVAILABLE")
        self.assertEqual(evidence["domain_classification"]["CAPITAL_FLOW"]["classification"], "DATA_UNAVAILABLE")

    def test_external_evidence_cannot_mutate_review_state_v2(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, review28_state())
            before = json.loads(state_path.read_text(encoding="utf-8"))
            build_external_market_evidence("ETH", SNAPSHOT_TS, {"oi": sample_metric("oi", 100)})
            after = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(before, after)

    def test_historical_snapshot_immutability(self) -> None:
        snapshot = self._opportunity_snapshot()
        before = copy.deepcopy(snapshot)
        external = build_external_market_evidence(snapshot["symbol"], snapshot["snapshot_timestamp"], {"oi_change_5m": sample_metric("oi_change_5m", 1.0, snapshot["snapshot_timestamp"])}, {"price_change_pct": 1.0})
        attached = attach_external_evidence_reference(snapshot, external)
        self.assertEqual(snapshot, before)
        self.assertIn("external_evidence_ref", attached)

    def test_feature_determinism(self) -> None:
        metrics = {"spot_cvd_change": sample_metric("spot_cvd_change", 3.0), "perp_cvd_change": sample_metric("perp_cvd_change", 2.0)}
        first = build_external_market_evidence("ETH", SNAPSHOT_TS, metrics)
        second = build_external_market_evidence("ETH", SNAPSHOT_TS, copy.deepcopy(metrics))
        self.assertEqual(first, second)

    def test_no_hindsight(self) -> None:
        snapshot = self._opportunity_snapshot()
        future = metric("funding_rate", 0.001, "fixture", snapshot["snapshot_timestamp"], snapshot["snapshot_timestamp"] + 60, "8h")
        with self.assertRaises(ValueError):
            build_external_market_evidence(snapshot["symbol"], snapshot["snapshot_timestamp"], {"funding_rate": future})

    def test_relation_features_are_experimental_and_unweighted(self) -> None:
        evidence = build_external_market_evidence("ETH", SNAPSHOT_TS, {"large_order_imbalance": sample_metric("large_order_imbalance", 0.7)}, {"price_change_pct": 1.0})
        for feature in evidence["relation_features"].values():
            self.assertEqual(feature["status"], "EXPERIMENTAL")
            self.assertNotIn("score", feature)
            self.assertNotIn("weight", feature)

    def _opportunity_snapshot(self) -> dict:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, review28_state())
            reviews = review_snapshot(REVIEW29, state_path)
            load_review_state(state_path)
        raw = load_snapshot(REVIEW29)
        frames = {timeframe: to_market_data_frame(raw["ETH"][timeframe]) for timeframe in TIMEFRAMES}
        return extract_opportunity_snapshot(reviews["ETH"], frames)


if __name__ == "__main__":
    unittest.main()
