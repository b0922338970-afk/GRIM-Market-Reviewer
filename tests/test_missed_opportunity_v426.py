from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer.missed_opportunity import (
    TRACKER_SCHEMA,
    append_snapshot,
    backfill_46_49,
    build_origin_candidate,
    calculate_horizon,
    create_tracker,
    deterministic_tracker_id,
    empty_store,
    is_eligible_origin,
    load_tracker_store,
    persist_tracker_store,
    record_production_conversion,
    relation_features,
    terminalize_tracker,
    update_horizon_outcomes,
    upsert_tracker,
)
from market_reviewer.model import Candle, MarketDataFrame


def evidence(**overrides: str) -> dict[str, str]:
    values = {
        "STRUCTURE": "POSITIVE",
        "LIQUIDITY": "NEUTRAL",
        "MOMENTUM": "POSITIVE",
        "LOCATION": "NEGATIVE",
        "FRESHNESS": "NOT_APPLICABLE",
        "REMAINING_OPPORTUNITY": "NEGATIVE",
        "POSITIONING": "POSITIVE",
        "CROWDING": "NEUTRAL",
        "LIQUIDATION_CONTEXT": "NEUTRAL",
        "REGIME": "TREND_CONTINUATION",
        "SWING_BIAS": "BULLISH",
    }
    values.update(overrides)
    return values


def candidate(
    *,
    observation_number: int = 46,
    timestamp: int = 1_000,
    price: float = 100.0,
    symbol: str = "BTC",
    direction: str = "LONG",
    production_sequence_state: str = "INVALIDATED",
    production_review_state: str = "NO_TRADE",
    opportunity_evidence: dict[str, str] | None = None,
    risk_signatures: list[str] | None = None,
    external_evidence: dict | None = None,
    new_legal_genesis_active: bool = False,
) -> dict:
    return build_origin_candidate(
        symbol=symbol,
        direction=direction,
        observation_number=observation_number,
        snapshot_timestamp=timestamp,
        price=price,
        production_sequence_id=f"{symbol}-seq-0009",
        production_sequence_state=production_sequence_state,
        production_review_state=production_review_state,
        opportunity_evidence=opportunity_evidence or evidence(),
        external_evidence=external_evidence,
        risk_signatures=risk_signatures,
        new_legal_genesis_active=new_legal_genesis_active,
    )


def candle(timestamp: int, high: float, low: float, close: float | None = None) -> Candle:
    close_value = close if close is not None else (high + low) / 2
    return Candle(timestamp=timestamp, open=close_value, high=high, low=low, close=close_value, volume=1)


def frame(candles: list[Candle], *, latest_closed: int | None = None) -> MarketDataFrame:
    latest = latest_closed if latest_closed is not None else candles[-1].timestamp
    return MarketDataFrame(
        symbol="BTC",
        timeframe="M5",
        source="fixture",
        provider="Coinbase",
        market_type="spot",
        timezone="UTC",
        dataset_id="test",
        generation_id="test",
        generated_at="2026-01-01T00:00:00+00:00",
        source_environment="test",
        completeness_status="DATA_READY",
        fetch_timestamp=latest + 300,
        latest_candle_timestamp=candles[-1].timestamp,
        latest_closed_candle_timestamp=latest,
        current_open_candle_timestamp=None,
        candles=candles,
        status="DATA_READY",
    )


def continuous_candles(start: int, count: int, *, high: float = 101.0, low: float = 99.0) -> list[Candle]:
    return [candle(start + 300 * index, high + index * 0.01, low - index * 0.01, 100.0) for index in range(count)]


class MissedOpportunityTrackerV426Tests(unittest.TestCase):
    def test_schema_version(self) -> None:
        self.assertEqual(empty_store("fixed")["schema"], TRACKER_SCHEMA)

    def test_origin_contract_accepts_positive_structure_momentum_and_positioning(self) -> None:
        self.assertTrue(is_eligible_origin(candidate()))

    def test_origin_contract_accepts_liquidity_without_positioning(self) -> None:
        c = candidate(opportunity_evidence=evidence(POSITIONING="NEUTRAL", LIQUIDITY="POSITIVE"))
        self.assertTrue(is_eligible_origin(c))

    def test_origin_contract_rejects_active_production_opportunity(self) -> None:
        c = candidate(production_sequence_state="RETEST_PENDING", production_review_state="WATCH")
        self.assertFalse(is_eligible_origin(c))

    def test_origin_contract_rejects_new_legal_genesis(self) -> None:
        self.assertFalse(is_eligible_origin(candidate(new_legal_genesis_active=True)))

    def test_origin_contract_rejects_missing_structure(self) -> None:
        self.assertFalse(is_eligible_origin(candidate(opportunity_evidence=evidence(STRUCTURE="NEUTRAL"))))

    def test_origin_contract_rejects_missing_momentum(self) -> None:
        self.assertFalse(is_eligible_origin(candidate(opportunity_evidence=evidence(MOMENTUM="NEUTRAL"))))

    def test_origin_contract_rejects_without_positioning_or_liquidity(self) -> None:
        c = candidate(opportunity_evidence=evidence(POSITIONING="NEUTRAL", LIQUIDITY="NEUTRAL"))
        self.assertFalse(is_eligible_origin(c))

    def test_origin_contract_rejects_hard_research_invalidation(self) -> None:
        self.assertFalse(is_eligible_origin(candidate(risk_signatures=["HARD_RESEARCH_INVALIDATION"])))

    def test_create_tracker_fields_are_research_only(self) -> None:
        record = create_tracker(candidate(), created_at="fixed")
        self.assertEqual(record["status"], "ACTIVE")
        self.assertEqual(record["origin_observation"], 46)
        self.assertEqual(len(record["snapshots"]), 1)

    def test_deterministic_tracker_id(self) -> None:
        c = candidate()
        one = deterministic_tracker_id(c["symbol"], c["direction"], c["snapshot_timestamp"], c["context_signature"])
        two = deterministic_tracker_id(c["symbol"], c["direction"], c["snapshot_timestamp"], c["context_signature"])
        self.assertEqual(one, two)

    def test_upsert_deduplicates_same_observation(self) -> None:
        store = empty_store("fixed")
        c = candidate()
        upsert_tracker(store, c, updated_at="fixed")
        upsert_tracker(store, c, updated_at="fixed")
        self.assertEqual(len(store["records"]), 1)
        self.assertEqual(len(store["records"][0]["snapshots"]), 1)

    def test_upsert_appends_later_snapshot_to_same_tracker(self) -> None:
        store = empty_store("fixed")
        upsert_tracker(store, candidate(timestamp=1_000, price=100), updated_at="fixed")
        upsert_tracker(store, candidate(observation_number=47, timestamp=1_300, price=101), updated_at="fixed")
        self.assertEqual(len(store["records"]), 1)
        self.assertEqual(len(store["records"][0]["snapshots"]), 2)

    def test_snapshots_do_not_create_independent_cases(self) -> None:
        store = empty_store("fixed")
        for obs, ts, price in ((46, 1_000, 100), (47, 1_300, 101), (48, 1_600, 102)):
            upsert_tracker(store, candidate(observation_number=obs, timestamp=ts, price=price), updated_at="fixed")
        self.assertEqual(len(store["records"]), 1)

    def test_append_snapshot_sets_improving(self) -> None:
        record = create_tracker(candidate(price=100), created_at="fixed")
        append_snapshot(record, candidate(observation_number=47, timestamp=1_300, price=101), updated_at="fixed")
        self.assertEqual(record["snapshots"][-1]["trajectory_state"], "IMPROVING")

    def test_append_snapshot_sets_deteriorating(self) -> None:
        record = create_tracker(candidate(price=100), created_at="fixed")
        append_snapshot(record, candidate(observation_number=47, timestamp=1_300, price=99), updated_at="fixed")
        self.assertEqual(record["status"], "DETERIORATING")
        self.assertEqual(record["snapshots"][-1]["trajectory_state"], "DETERIORATING")

    def test_candidate_input_immutable(self) -> None:
        c = candidate()
        before = copy.deepcopy(c)
        create_tracker(c, created_at="fixed")
        self.assertEqual(c, before)

    def test_persist_and_reload_research_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            store = empty_store("fixed")
            upsert_tracker(store, candidate(), updated_at="fixed")
            persist_tracker_store(path, store)
            self.assertEqual(load_tracker_store(path), store)

    def test_persistence_does_not_touch_review_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            review_state = Path(tmp) / "reviews" / "thesis-baseline.json"
            review_state.parent.mkdir()
            review_state.write_text('{"production":"unchanged"}', encoding="utf-8")
            store = empty_store("fixed")
            upsert_tracker(store, candidate(), updated_at="fixed")
            persist_tracker_store(Path(tmp) / "research" / "missed-opportunities.json", store)
            self.assertEqual(review_state.read_text(encoding="utf-8"), '{"production":"unchanged"}')

    def test_long_mfe_mae_complete(self) -> None:
        candles = [candle(1_300, 110, 95)] + continuous_candles(1_600, 11)
        result = calculate_horizon(direction="LONG", origin_timestamp=1_000, origin_price=100, frame=frame(candles, latest_closed=4_600), horizon="1H")
        self.assertEqual(result["horizon_status"], "COMPLETE")
        self.assertAlmostEqual(result["MFE_pct"], 10.0)
        self.assertAlmostEqual(result["MAE_pct"], -5.0)

    def test_short_mfe_mae_complete(self) -> None:
        candles = [candle(1_300, 105, 90)] + continuous_candles(1_600, 11)
        result = calculate_horizon(direction="SHORT", origin_timestamp=1_000, origin_price=100, frame=frame(candles, latest_closed=4_600), horizon="1H")
        self.assertEqual(result["horizon_status"], "COMPLETE")
        self.assertAlmostEqual(result["MFE_pct"], (100 / 90 - 1) * 100)
        self.assertAlmostEqual(result["MAE_pct"], (100 / 105 - 1) * 100)

    def test_incomplete_horizon_pending(self) -> None:
        result = calculate_horizon(direction="LONG", origin_timestamp=1_000, origin_price=100, frame=frame(continuous_candles(1_300, 4), latest_closed=2_200), horizon="1H")
        self.assertEqual(result["horizon_status"], "PENDING")

    def test_data_gap_when_window_elapsed_but_missing_candles(self) -> None:
        result = calculate_horizon(direction="LONG", origin_timestamp=1_000, origin_price=100, frame=frame([candle(1_300, 101, 99)], latest_closed=4_600), horizon="1H")
        self.assertEqual(result["horizon_status"], "DATA_GAP")

    def test_open_candle_not_used_for_outcome(self) -> None:
        candles = continuous_candles(1_300, 12, high=101, low=99) + [candle(4_900, 150, 50)]
        result = calculate_horizon(direction="LONG", origin_timestamp=1_000, origin_price=100, frame=frame(candles, latest_closed=4_600), horizon="1H")
        self.assertLess(result["MFE_price"], 150)
        self.assertGreater(result["MAE_price"], 50)

    def test_four_hour_horizon_complete(self) -> None:
        result = calculate_horizon(direction="LONG", origin_timestamp=1_000, origin_price=100, frame=frame(continuous_candles(1_300, 48), latest_closed=15_400), horizon="4H")
        self.assertEqual(result["horizon_status"], "COMPLETE")

    def test_twelve_hour_horizon_complete(self) -> None:
        result = calculate_horizon(direction="LONG", origin_timestamp=1_000, origin_price=100, frame=frame(continuous_candles(1_300, 144), latest_closed=44_200), horizon="12H")
        self.assertEqual(result["horizon_status"], "COMPLETE")

    def test_twenty_four_hour_horizon_complete(self) -> None:
        result = calculate_horizon(direction="LONG", origin_timestamp=1_000, origin_price=100, frame=frame(continuous_candles(1_300, 288), latest_closed=87_400), horizon="24H")
        self.assertEqual(result["horizon_status"], "COMPLETE")

    def test_update_horizons_marks_outcome_complete(self) -> None:
        record = create_tracker(candidate(timestamp=1_000, price=100), created_at="fixed")
        update_horizon_outcomes(record, frame(continuous_candles(1_300, 288), latest_closed=87_400))
        self.assertEqual(record["status"], "OUTCOME_COMPLETE")
        self.assertEqual(record["terminal_reason"], "MAX_HORIZON_COMPLETE")

    def test_production_conversion_recorded_without_failure(self) -> None:
        record = create_tracker(candidate(timestamp=1_000, price=100), created_at="fixed")
        record_production_conversion(record, "BTC-seq-0010", 1_900, 103)
        self.assertTrue(record["converted_to_production"])
        self.assertEqual(record["status"], "CONVERTED")
        self.assertEqual(record["terminal_reason"], "NEW_PRODUCTION_SEQUENCE_STARTED")

    def test_terminal_context_invalidation(self) -> None:
        record = create_tracker(candidate(), created_at="fixed")
        terminalize_tracker(record, "CONTEXT_INVALIDATED")
        self.assertEqual(record["status"], "TERMINAL")
        self.assertEqual(record["terminal_reason"], "CONTEXT_INVALIDATED")

    def test_relation_features_are_research_only(self) -> None:
        c = candidate(external_evidence={"oi_delta_1h": 10, "oi_delta_4h": 5})
        self.assertEqual(relation_features(c), ["POSITIONING_REBUILD"])

    def test_relation_features_price_down_oi_up(self) -> None:
        c = candidate(price=99, external_evidence={"oi_delta_1h": 10})
        self.assertEqual(relation_features(c, previous_price=100), ["PRICE_DOWN_OI_UP"])

    def test_relation_features_price_up_oi_up(self) -> None:
        c = candidate(price=101, external_evidence={"oi_delta_1h": 10})
        self.assertEqual(relation_features(c, previous_price=100), ["PRICE_UP_OI_UP"])

    def test_relation_features_complete_zero_liquidation_context(self) -> None:
        c = candidate(external_evidence={"liquidation_5m": {"long": 0, "short": 0, "coverage_status": "COMPLETE"}})
        self.assertIn("ZERO_LIQUIDATION_CONTEXT", relation_features(c))

    def test_relation_features_incomplete_zero_liquidation_not_neutral(self) -> None:
        c = candidate(external_evidence={"liquidation_5m": {"long": 0, "short": 0, "coverage_status": "PARTIAL"}})
        self.assertNotIn("ZERO_LIQUIDATION_CONTEXT", relation_features(c))

    def test_store_rejects_execution_keys(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = empty_store("fixed")
            store["records"].append({"entry": 1})
            with self.assertRaises(ValueError):
                persist_tracker_store(Path(tmp) / "research" / "missed-opportunities.json", store)

    def test_backfill_creates_btc_and_eth_trackers(self) -> None:
        store = backfill_46_49(generated_at="fixed")
        self.assertEqual(len(store["records"]), 2)
        self.assertEqual({record["symbol"] for record in store["records"]}, {"BTC", "ETH"})

    def test_backfill_btc_expected_prices_and_trajectory(self) -> None:
        btc = next(record for record in backfill_46_49("fixed")["records"] if record["symbol"] == "BTC")
        prices = [snapshot["price"] for snapshot in btc["snapshots"]]
        states = [snapshot["trajectory_state"] for snapshot in btc["snapshots"]]
        self.assertEqual(prices, [79590.12, 80099.75, 80437.92, 80335.99])
        self.assertEqual(states, ["STABLE", "IMPROVING", "IMPROVING", "DETERIORATING"])
        self.assertAlmostEqual(btc["snapshots"][2]["price_change_from_origin_pct"], 1.065209, places=5)
        self.assertAlmostEqual(btc["snapshots"][3]["price_change_from_origin_pct"], 0.93714, places=5)

    def test_backfill_eth_expected_prices_and_trajectory(self) -> None:
        eth = next(record for record in backfill_46_49("fixed")["records"] if record["symbol"] == "ETH")
        prices = [snapshot["price"] for snapshot in eth["snapshots"]]
        states = [snapshot["trajectory_state"] for snapshot in eth["snapshots"]]
        self.assertEqual(prices, [2505.37, 2513.54, 2527.80, 2520.19])
        self.assertEqual(states, ["STABLE", "IMPROVING", "IMPROVING", "DETERIORATING"])
        self.assertAlmostEqual(eth["snapshots"][2]["price_change_from_origin_pct"], 0.895276, places=5)
        self.assertAlmostEqual(eth["snapshots"][3]["price_change_from_origin_pct"], 0.591529, places=5)

    def test_backfill_conversion_no(self) -> None:
        store = backfill_46_49("fixed")
        self.assertTrue(all(record["converted_to_production"] is False for record in store["records"]))

    def test_backfill_historical_external_remains_null(self) -> None:
        store = backfill_46_49("fixed")
        for record in store["records"]:
            for snapshot in record["snapshots"]:
                external = snapshot["external_evidence"]
                self.assertIsNone(external["oi_current"])
                self.assertIsNone(external["funding_current"])
                self.assertIn("historical external raw fields were not persisted", external["provenance"])

    def test_no_score_grade_entry_sl_tp_generated(self) -> None:
        payload = json.dumps(backfill_46_49("fixed"), sort_keys=True).lower()
        for forbidden in ('"score"', '"grade"', '"entry"', '"sl"', '"tp"'):
            self.assertNotIn(forbidden, payload)

    def test_historical_snapshot_not_rewritten_by_later_snapshot(self) -> None:
        record = create_tracker(candidate(timestamp=1_000, price=100), created_at="fixed")
        original = copy.deepcopy(record["snapshots"][0])
        append_snapshot(record, candidate(observation_number=47, timestamp=1_300, price=101), updated_at="fixed")
        self.assertEqual(record["snapshots"][0], original)

    def test_recovered_horizons_include_outcome_only_provenance(self) -> None:
        record = create_tracker(candidate(timestamp=1_000, price=100), created_at="fixed")
        update_horizon_outcomes(record, frame(continuous_candles(1_300, 288), latest_closed=87_400))
        for horizon in ("1H", "4H", "12H", "24H"):
            outcome = record["outcomes"][horizon]
            self.assertEqual(outcome["horizon_status"], "COMPLETE")
            self.assertEqual(outcome["outcome_source"], "historical_outcome_recovery")
            self.assertTrue(outcome["outcome_coverage_complete"])

    def test_partial_recovery_remains_data_gap(self) -> None:
        result = calculate_horizon(direction="LONG", origin_timestamp=1_000, origin_price=100, frame=frame(continuous_candles(1_300, 11), latest_closed=4_600), horizon="1H")
        self.assertEqual(result["horizon_status"], "DATA_GAP")
        self.assertFalse(result["outcome_coverage_complete"])

    def test_recovered_data_updates_outcomes_only(self) -> None:
        record = create_tracker(candidate(timestamp=1_000, price=100), created_at="fixed")
        snapshots_before = copy.deepcopy(record["snapshots"])
        update_horizon_outcomes(record, frame(continuous_candles(1_300, 288), latest_closed=87_400))
        self.assertEqual(record["snapshots"], snapshots_before)

    def test_recovery_idempotent(self) -> None:
        record = create_tracker(candidate(timestamp=1_000, price=100), created_at="fixed")
        data = frame(continuous_candles(1_300, 288), latest_closed=87_400)
        update_horizon_outcomes(record, data)
        first = copy.deepcopy(record["outcomes"])
        update_horizon_outcomes(record, data)
        self.assertEqual(record["outcomes"], first)

    def test_restart_reload_recovered_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            store = empty_store("fixed")
            record = create_tracker(candidate(timestamp=1_000, price=100), created_at="fixed")
            update_horizon_outcomes(record, frame(continuous_candles(1_300, 288), latest_closed=87_400))
            store["records"].append(record)
            persist_tracker_store(path, store)
            loaded = load_tracker_store(path)
            self.assertEqual(loaded["records"][0]["outcomes"], record["outcomes"])


if __name__ == "__main__":
    unittest.main()