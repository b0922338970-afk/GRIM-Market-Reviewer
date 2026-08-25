from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer.external_evidence import build_external_market_evidence
from market_reviewer.external_evidence_providers import (
    BinanceUSDmExternalEvidenceProvider,
    LIQUIDATION_HISTORY_LIVE_ONLY,
    build_phase1_external_evidence_artifact,
    instrument_mapping,
    normalize_funding,
    normalize_liquidation_flow,
    normalize_open_interest,
    phase1_metric,
    publish_external_evidence_artifact,
)
from market_reviewer.persistence import atomic_write_json
from market_reviewer.pipeline import review_snapshot
from tests.test_opportunity_v42 import REVIEW29, review28_state


SNAPSHOT_TS = 1_800_000_000


class ExternalEvidenceProviderV424Tests(unittest.TestCase):
    def mapping(self):
        return instrument_mapping("BTC")

    def oi_history(self) -> list[dict]:
        base = (SNAPSHOT_TS - 48 * 300) * 1000
        return [
            {"timestamp": base + index * 300_000, "sumOpenInterest": str(1000 + index), "sumOpenInterestValue": str(10_000 + index * 100)}
            for index in range(49)
        ]

    def test_oi_normalization(self) -> None:
        mapping = self.mapping()
        metrics = normalize_open_interest(mapping, {"openInterest": "123.5", "time": SNAPSHOT_TS * 1000}, self.oi_history(), SNAPSHOT_TS)
        self.assertEqual(metrics["oi"]["provider"], "binance_usdm_futures")
        self.assertEqual(metrics["oi"]["instrument"], "BTCUSDT")
        self.assertEqual(metrics["oi"]["normalized_unit"], "PROVIDER_CONTRACTS")
        self.assertEqual(metrics["oi"]["value"], 123.5)

    def test_oi_change_windows(self) -> None:
        metrics = normalize_open_interest(self.mapping(), {"openInterest": "123.5", "time": SNAPSHOT_TS * 1000}, self.oi_history(), SNAPSHOT_TS)
        self.assertEqual(metrics["oi_change_5m"]["value"], 100.0)
        self.assertEqual(metrics["oi_change_15m"]["value"], 300.0)
        self.assertEqual(metrics["oi_change_1h"]["value"], 1200.0)
        self.assertEqual(metrics["oi_change_4h"]["value"], 4800.0)

    def test_contract_unit_preservation(self) -> None:
        metrics = normalize_open_interest(self.mapping(), {"openInterest": "2.5", "time": SNAPSHOT_TS * 1000}, [], SNAPSHOT_TS)
        self.assertEqual(metrics["oi"]["raw_unit"], "PROVIDER_CONTRACTS")
        self.assertEqual(metrics["oi"]["raw_value"], 2.5)
        self.assertEqual(metrics["oi"]["normalized_unit"], "PROVIDER_CONTRACTS")

    def test_funding_settled_vs_predicted_distinction(self) -> None:
        metrics = normalize_funding(
            self.mapping(),
            {"lastFundingRate": "0.0001", "nextFundingTime": (SNAPSHOT_TS + 3600) * 1000, "time": SNAPSHOT_TS * 1000},
            [{"fundingRate": "0.00005", "fundingTime": (SNAPSHOT_TS - 28_800) * 1000}],
            SNAPSHOT_TS,
        )
        self.assertEqual(metrics["funding_rate"]["semantics"], "CURRENT_OR_PREDICTED")
        self.assertEqual(metrics["next_funding_timestamp"]["semantics"], "PREDICTED")

    def test_insufficient_funding_history_is_partial_or_unavailable(self) -> None:
        metrics = normalize_funding(self.mapping(), None, [{"fundingRate": "0.00005", "fundingTime": SNAPSHOT_TS * 1000}], SNAPSHOT_TS)
        self.assertEqual(metrics["funding_percentile"]["availability"], "PARTIAL")
        empty = normalize_funding(self.mapping(), None, [], SNAPSHOT_TS)
        self.assertEqual(empty["funding_percentile"]["availability"], "UNAVAILABLE")

    def test_liquidation_long_aggregation(self) -> None:
        metrics, capability = normalize_liquidation_flow(self.mapping(), [{"side": "LONG_LIQUIDATION", "price": 100, "quantity": 2, "event_timestamp": SNAPSHOT_TS - 60}], SNAPSHOT_TS)
        self.assertEqual(capability, LIQUIDATION_HISTORY_LIVE_ONLY)
        self.assertEqual(metrics["long_liquidation_notional_5m"]["value"], 200.0)
        self.assertEqual(metrics["short_liquidation_notional_5m"]["value"], 0)

    def test_liquidation_short_aggregation(self) -> None:
        metrics, _ = normalize_liquidation_flow(self.mapping(), [{"side": "SHORT_LIQUIDATION", "price": 50, "quantity": 3, "event_timestamp": SNAPSHOT_TS - 120}], SNAPSHOT_TS)
        self.assertEqual(metrics["short_liquidation_notional_5m"]["value"], 150.0)
        self.assertEqual(metrics["long_liquidation_notional_5m"]["value"], 0)

    def test_liquidation_live_only_capability_semantics(self) -> None:
        metrics, capability = normalize_liquidation_flow(self.mapping(), None, SNAPSHOT_TS)
        self.assertEqual(capability, "LIVE_ONLY")
        self.assertEqual(metrics["long_liquidation_notional_5m"]["availability"], "UNAVAILABLE")

    def test_stale_oi_marked_stale(self) -> None:
        item = phase1_metric("oi", 100, self.mapping(), SNAPSHOT_TS - 600, SNAPSHOT_TS - 600, SNAPSHOT_TS, "current", "CONTRACTS", "CONTRACTS")
        self.assertEqual(item["availability"], "STALE")

    def test_stale_funding_handled_correctly(self) -> None:
        item = phase1_metric("funding_rate", 0.0001, self.mapping(), SNAPSHOT_TS - 40_000, SNAPSHOT_TS - 40_000, SNAPSHOT_TS, "current", "RATE", "RATE")
        self.assertEqual(item["availability"], "STALE")
        evidence = build_external_market_evidence("BTC", SNAPSHOT_TS, {"funding_rate": item})
        self.assertEqual(evidence["domain_classification"]["CROWDING"]["classification"], "DATA_UNAVAILABLE")

    def test_stale_liquidation_not_treated_as_current(self) -> None:
        item = phase1_metric("long_liquidation_notional_5m", 10_000, self.mapping(), SNAPSHOT_TS - 900, SNAPSHOT_TS - 900, SNAPSHOT_TS, "5m", "USD", "USD")
        self.assertEqual(item["availability"], "STALE")
        evidence = build_external_market_evidence("BTC", SNAPSHOT_TS, {"long_liquidation_notional_5m": item})
        self.assertEqual(evidence["relation_features"]["LIQUIDATION_CONTEXT_RELATION"]["value"]["relation"], "DATA_UNAVAILABLE")

    def test_provider_identity_retained(self) -> None:
        item = phase1_metric("oi", 1, self.mapping(), SNAPSHOT_TS, SNAPSHOT_TS, SNAPSHOT_TS, "current", "CONTRACTS", "CONTRACTS")
        self.assertEqual(item["provider"], "binance_usdm_futures")
        self.assertEqual(item["market_type"], "usdt_perpetual")
        self.assertEqual(item["symbol"], "BTC")
        self.assertEqual(item["instrument"], "BTCUSDT")

    def test_cross_source_price_and_derivatives_timestamps_align(self) -> None:
        derivative = phase1_metric("oi_change_5m", 5, self.mapping(), SNAPSHOT_TS - 1, SNAPSHOT_TS - 1, SNAPSHOT_TS, "5m", "CONTRACTS", "DELTA")
        evidence = build_external_market_evidence("BTC", SNAPSHOT_TS, {"oi_change_5m": derivative}, {"price_change_pct": 1.0})
        self.assertEqual(evidence["relation_features"]["OI_PRICE_RELATION"]["value"]["relation"], "PRICE_UP_OI_UP")

    def test_unavailable_external_feed_does_not_block_production(self) -> None:
        class FailingProvider:
            name = "binance_usdm_futures"
            liquidation_history = "LIVE_ONLY"

            def fetch_metrics(self, symbol: str, fetch_timestamp: int | None = None):
                raise OSError("network")

        artifact = build_phase1_external_evidence_artifact(("BTC",), SNAPSHOT_TS, FailingProvider())
        self.assertEqual(artifact["external_evidence_status"], "DATA_UNAVAILABLE")
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, review28_state())
            reviews = review_snapshot(REVIEW29, state_path)
        self.assertIn("BTC", reviews)

    def test_partial_phase1_feed_classified_partial(self) -> None:
        class OIOnlyProvider:
            name = "binance_usdm_futures"
            liquidation_history = "LIVE_ONLY"

            def fetch_metrics(self, symbol: str, fetch_timestamp: int | None = None):
                mapping = instrument_mapping(symbol)
                return {"oi_change_5m": phase1_metric("oi_change_5m", 1, mapping, SNAPSHOT_TS, SNAPSHOT_TS, SNAPSHOT_TS, "5m", "CONTRACTS", "DELTA")}, {"provider": self.name, "instrument": mapping.instrument, "market_type": mapping.market_type, "liquidation_history": "LIVE_ONLY"}

        artifact = build_phase1_external_evidence_artifact(("BTC",), SNAPSHOT_TS, OIOnlyProvider())
        self.assertEqual(artifact["external_evidence_status"], "PARTIAL")
        self.assertEqual(artifact["symbols"]["BTC"]["diagnostics"]["external_evidence_status"], "PARTIAL")

    def test_capital_flow_remains_data_unavailable(self) -> None:
        evidence = build_external_market_evidence("BTC", SNAPSHOT_TS, {"oi_change_5m": phase1_metric("oi_change_5m", 1, self.mapping(), SNAPSHOT_TS, SNAPSHOT_TS, SNAPSHOT_TS, "5m", "CONTRACTS", "DELTA")}, {"price_change_pct": 1})
        self.assertEqual(evidence["domain_classification"]["POSITIONING"]["classification"], "POSITIVE")
        self.assertEqual(evidence["domain_classification"]["CAPITAL_FLOW"]["classification"], "DATA_UNAVAILABLE")

    def test_no_hindsight(self) -> None:
        future = phase1_metric("oi_change_5m", 1, self.mapping(), SNAPSHOT_TS + 1, SNAPSHOT_TS + 1, SNAPSHOT_TS + 1, "5m", "CONTRACTS", "DELTA")
        with self.assertRaises(ValueError):
            build_external_market_evidence("BTC", SNAPSHOT_TS, {"oi_change_5m": future})

    def test_historical_snapshot_immutability(self) -> None:
        artifact = build_phase1_external_evidence_artifact(("BTC",), SNAPSHOT_TS, StaticProvider())
        before = copy.deepcopy(artifact)
        publish_dir = tempfile.TemporaryDirectory()
        try:
            path = publish_external_evidence_artifact(artifact, Path(publish_dir.name))
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), artifact)
            self.assertEqual(artifact, before)
        finally:
            publish_dir.cleanup()

    def test_production_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, review28_state())
            before = json.loads(state_path.read_text(encoding="utf-8"))
            build_phase1_external_evidence_artifact(("BTC", "ETH"), SNAPSHOT_TS, StaticProvider())
            after = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(before, after)

    def test_adapter_normalizes_fixture_payloads(self) -> None:
        adapter = BinanceUSDmExternalEvidenceProvider(get_json=FixtureGetJson())
        artifact = build_phase1_external_evidence_artifact(("BTC",), SNAPSHOT_TS, adapter)
        symbol_payload = artifact["symbols"]["BTC"]
        self.assertEqual(symbol_payload["diagnostics"]["provider"], "binance_usdm_futures")
        self.assertEqual(symbol_payload["evidence"]["raw_metrics"]["oi"]["availability"], "AVAILABLE")
        self.assertEqual(symbol_payload["evidence"]["raw_metrics"]["funding_rate"]["availability"], "AVAILABLE")
        self.assertEqual(symbol_payload["diagnostics"]["liquidation_history"], "LIVE_ONLY")


class StaticProvider:
    name = "binance_usdm_futures"
    liquidation_history = "LIVE_ONLY"

    def fetch_metrics(self, symbol: str, fetch_timestamp: int | None = None):
        timestamp = SNAPSHOT_TS if fetch_timestamp is None else fetch_timestamp
        mapping = instrument_mapping(symbol)
        metrics = {
            "oi_change_5m": phase1_metric("oi_change_5m", 1, mapping, timestamp, timestamp, timestamp, "5m", "CONTRACTS", "DELTA"),
            "funding_rate": phase1_metric("funding_rate", 0.0001, mapping, timestamp, timestamp, timestamp, "current", "RATE", "RATE"),
        }
        return metrics, {"provider": self.name, "instrument": mapping.instrument, "market_type": mapping.market_type, "liquidation_history": "LIVE_ONLY"}


class FixtureGetJson:
    def __call__(self, url: str):
        if "openInterest?" in url:
            return {"openInterest": "1000", "time": SNAPSHOT_TS * 1000}
        if "openInterestHist" in url:
            base = (SNAPSHOT_TS - 48 * 300) * 1000
            return [{"timestamp": base + index * 300_000, "sumOpenInterest": str(100 + index), "sumOpenInterestValue": str(1000 + index * 10)} for index in range(49)]
        if "premiumIndex" in url:
            return {"lastFundingRate": "0.0001", "nextFundingTime": (SNAPSHOT_TS + 3600) * 1000, "time": SNAPSHOT_TS * 1000}
        if "fundingRate" in url:
            return [{"fundingRate": str(0.00001 * index), "fundingTime": (SNAPSHOT_TS - (40 - index) * 28_800) * 1000} for index in range(40)]
        raise OSError("unexpected url")


if __name__ == "__main__":
    unittest.main()