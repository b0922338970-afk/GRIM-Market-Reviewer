from __future__ import annotations

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer.cli import main
from market_reviewer.external_evidence import build_external_market_evidence, metric
from market_reviewer.missed_opportunity import backfill_46_49, empty_store, load_tracker_store, persist_tracker_store
from market_reviewer.missed_opportunity_live import (
    apply_missed_opportunity_observation,
    dry_run_missed_opportunity_observation,
    explicit_backfill_v426,
    external_tracker_fields,
    load_or_initialize_research_store,
    missed_opportunity_status,
    opportunity_evidence_from_snapshot,
    price_change_context_from_frames,
    tracker_health,
)
from market_reviewer.model import Candle, MarketDataFrame
from market_reviewer.persistence import atomic_write_json


SNAPSHOT_TS = 1787850300


def candle(timestamp: int, close: float, high: float | None = None, low: float | None = None) -> Candle:
    return Candle(timestamp=timestamp, open=close, high=high if high is not None else close + 10, low=low if low is not None else close - 10, close=close, volume=1)


def frame(symbol: str, start: int = 1787828100, count: int = 60, price: float = 100.0, latest_closed: int | None = None) -> MarketDataFrame:
    candles = [candle(start + index * 300, price + index, high=price + index + 5, low=price + index - 5) for index in range(count)]
    latest = latest_closed if latest_closed is not None else candles[-1].timestamp
    return MarketDataFrame(
        symbol=symbol,
        timeframe="M5",
        source="fixture",
        provider="Coinbase",
        market_type="spot",
        timezone="UTC",
        dataset_id="test",
        generation_id="gen",
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


def frames(symbol: str = "BTC", price: float = 100.0, count: int = 60) -> dict[str, MarketDataFrame]:
    m5 = frame(symbol, price=price, count=count)
    return {tf: m5 for tf in ("D1", "H4", "H1", "M15", "M5")}


def terminal_review(symbol: str = "BTC", state: str = "INVALIDATED", price: float = 100.0) -> dict:
    return {
        "Symbol": symbol,
        "Sequence_ID": f"{symbol}-seq-0009",
        "Sequence_State": state,
        "State": "NO_TRADE",
        "Swing_Bias": "BULLISH",
        "Review_Timestamp": str(SNAPSHOT_TS),
        "Sequence_Transitions": [],
    }


def genesis_review(symbol: str = "BTC") -> dict:
    review = terminal_review(symbol, state="SEEKING_LIQUIDITY")
    review["Sequence_ID"] = f"{symbol}-seq-0010"
    review["State"] = "WAIT"
    review["Sequence_Transitions"] = [
        {"previous_state": "NONE", "new_state": "SEEKING_LIQUIDITY", "timestamp": SNAPSHOT_TS, "evidence": "pullback genesis"}
    ]
    return review


def opportunity(symbol: str = "BTC", htf: int = 2, momentum: str = "MATURE_CONTINUATION", sequence_state: str = "INVALIDATED") -> dict:
    return {
        "symbol": symbol,
        "sequence_id": f"{symbol}-seq-0009",
        "sequence_state": sequence_state,
        "snapshot_timestamp": SNAPSHOT_TS,
        "opportunity_status": "NO_ACTIVE_OPPORTUNITY" if sequence_state in {"INVALIDATED", "EXPIRED_NO_TRIGGER"} else "ACTIVE_OPPORTUNITY",
        "truth": {"swing_bias": "BULLISH", "active_sweep": False},
        "raw_metrics": {"LIQUIDITY": {"liquidity_distance_pct": None}},
        "features": {
            "HTF_ALIGNMENT": {"value": {"aligned_count": htf}},
            "DISPLACEMENT_STRENGTH": {"value": {"status": "MISSING"}},
            "TREND_MATURITY": {"value": {"trend_maturity": momentum}},
            "POI_FRESHNESS": {"value": {"status": "DATA_UNAVAILABLE"}},
            "REGIME": {"value": {"regime": "TREND_CONTINUATION"}},
        },
        "risk_signatures": [],
    }


def external(symbol: str = "BTC", snapshot_timestamp: int = SNAPSHOT_TS) -> dict:
    metrics = {
        "oi_change_5m": metric("oi_change_5m", 5, "fixture", snapshot_timestamp, snapshot_timestamp, "5m"),
        "oi_change_1h": metric("oi_change_1h", 10, "fixture", snapshot_timestamp, snapshot_timestamp, "1h"),
        "funding_rate": metric("funding_rate", 0.0001, "fixture", snapshot_timestamp, snapshot_timestamp, "current"),
        "long_liquidation_notional_5m": metric("long_liquidation_notional_5m", 0, "fixture", snapshot_timestamp, snapshot_timestamp, "5m"),
        "short_liquidation_notional_5m": metric("short_liquidation_notional_5m", 0, "fixture", snapshot_timestamp, snapshot_timestamp, "5m"),
    }
    return build_external_market_evidence(symbol, snapshot_timestamp, metrics, {"price_change_pct": 1.0})


def apply_once(store_path: Path, symbol: str = "BTC", review: dict | None = None, opp: dict | None = None, price: float = 100.0, persist_func=persist_tracker_store) -> dict:
    return apply_missed_opportunity_observation(
        reviews={symbol: review or terminal_review(symbol)},
        frames={symbol: frames(symbol, price=price)},
        opportunity_snapshots={symbol: opp or opportunity(symbol)},
        external_evidence={symbol: external(symbol)},
        observation_number=50,
        store_path=store_path,
        persist=True,
        persist_func=persist_func,
    )


class MissedOpportunityLiveV426aTests(unittest.TestCase):
    def test_live_observation_creates_eligible_tracker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = apply_once(Path(tmp) / "research" / "missed-opportunities.json")
            self.assertEqual(report["research_persistence"], "PASS")
            self.assertEqual(report["symbols"]["BTC"]["snapshot_count"], 1)

    def test_ineligible_observation_creates_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            report = apply_once(Path(tmp) / "research" / "missed-opportunities.json", opp=opportunity(htf=0, momentum="UNALIGNED"))
            self.assertEqual(report["symbols"]["BTC"]["tracker_active"], "NO")
            self.assertEqual(missed_opportunity_status(Path(tmp) / "research" / "missed-opportunities.json")["record_count"], 0)

    def test_existing_tracker_receives_new_snapshot(self) -> None:
        store = backfill_46_49("fixed")
        updated, report = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": terminal_review("BTC")}, frames={"BTC": frames("BTC", price=80350)}, opportunity_snapshots={"BTC": opportunity("BTC")}, external_evidence={"BTC": external("BTC")}, observation_number=50)
        btc = next(r for r in updated["records"] if r["symbol"] == "BTC")
        self.assertEqual(len(btc["snapshots"]), 5)
        self.assertEqual(report["symbols"]["BTC"]["current_snapshot_added"], "YES")

    def test_same_observation_rerun_does_not_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            apply_once(path)
            apply_once(path)
            self.assertEqual(missed_opportunity_status(path)["records"][0]["horizon_statuses"]["1H"], "PENDING")
            self.assertEqual(load_tracker_store(path)["records"][0]["snapshots"].__len__(), 1)

    def test_research_store_missing_initializes_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store, status = load_or_initialize_research_store(Path(tmp) / "research" / "missed-opportunities.json")
            self.assertEqual(status, "INITIALIZED_EMPTY")
            self.assertEqual(store["schema"], "missed-opportunity-tracker.v1")

    def test_malformed_research_store_fails_research_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            path.parent.mkdir()
            path.write_text('{"schema":"bad","records":[]}', encoding="utf-8")
            report = apply_once(path)
            self.assertEqual(report["research_persistence"], "FAIL")
            self.assertEqual(report["production_observation_impact"], "NONE")

    def test_production_remains_valid_if_research_persist_fails(self) -> None:
        def fail(path, store):
            raise OSError("disk full")
        with tempfile.TemporaryDirectory() as tmp:
            report = apply_once(Path(tmp) / "research" / "missed-opportunities.json", persist_func=fail)
            self.assertEqual(report["research_persistence"], "FAIL")
            self.assertEqual(report["production_observation_impact"], "NONE")

    def test_research_failure_does_not_mutate_review_state_v2(self) -> None:
        def fail(path, store):
            raise OSError("disk full")
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "reviews" / "thesis-baseline.json"
            atomic_write_json(state, {"persistence_version": 2, "symbols": {}})
            before = state.read_text(encoding="utf-8")
            apply_once(Path(tmp) / "research" / "missed-opportunities.json", persist_func=fail)
            self.assertEqual(state.read_text(encoding="utf-8"), before)

    def test_explicit_backfill_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            first = explicit_backfill_v426(path)
            second = explicit_backfill_v426(path)
            self.assertEqual(first["record_count"], 2)
            self.assertEqual(second["record_count"], 2)
            self.assertEqual(second["added_records"], 0)

    def test_btc_update_after_backfill_uses_same_tracker(self) -> None:
        store = backfill_46_49("fixed")
        btc_id = next(r["tracker_id"] for r in store["records"] if r["symbol"] == "BTC")
        updated, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": terminal_review("BTC")}, frames={"BTC": frames("BTC", price=80350)}, opportunity_snapshots={"BTC": opportunity("BTC")}, external_evidence={"BTC": external("BTC")}, observation_number=50)
        btc = next(r for r in updated["records"] if r["symbol"] == "BTC")
        self.assertEqual(btc["tracker_id"], btc_id)
        self.assertEqual(len(btc["snapshots"]), 5)

    def test_eth_update_after_backfill_uses_same_tracker(self) -> None:
        store = backfill_46_49("fixed")
        eth_id = next(r["tracker_id"] for r in store["records"] if r["symbol"] == "ETH")
        updated, _ = dry_run_missed_opportunity_observation(store=store, reviews={"ETH": terminal_review("ETH")}, frames={"ETH": frames("ETH", price=2521)}, opportunity_snapshots={"ETH": opportunity("ETH")}, external_evidence={"ETH": external("ETH")}, observation_number=50)
        eth = next(r for r in updated["records"] if r["symbol"] == "ETH")
        self.assertEqual(eth["tracker_id"], eth_id)
        self.assertEqual(len(eth["snapshots"]), 5)

    def test_no_silent_historical_backfill_during_live_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            apply_once(path)
            store = load_tracker_store(path)
            self.assertEqual(len(store["records"]), 1)
            self.assertEqual(store["records"][0]["origin_observation"], 50)

    def test_one_hour_outcome_updates_when_ready(self) -> None:
        store = backfill_46_49("fixed")
        updated, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": terminal_review("BTC")}, frames={"BTC": frames("BTC", price=80000, count=60)}, opportunity_snapshots={"BTC": opportunity("BTC")}, external_evidence={"BTC": external("BTC")}, observation_number=50)
        btc = next(r for r in updated["records"] if r["symbol"] == "BTC")
        self.assertIn(btc["outcomes"]["1H"]["horizon_status"], {"COMPLETE", "DATA_GAP"})

    def test_four_hour_outcome_updates_when_ready(self) -> None:
        store = backfill_46_49("fixed")
        updated, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": terminal_review("BTC")}, frames={"BTC": frames("BTC", price=80000, count=120)}, opportunity_snapshots={"BTC": opportunity("BTC")}, external_evidence={"BTC": external("BTC")}, observation_number=50)
        btc = next(r for r in updated["records"] if r["symbol"] == "BTC")
        self.assertIn(btc["outcomes"]["4H"]["horizon_status"], {"COMPLETE", "DATA_GAP"})

    def test_twelve_hour_stays_pending_if_not_ready(self) -> None:
        store = backfill_46_49("fixed")
        updated, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": terminal_review("BTC")}, frames={"BTC": frames("BTC", count=60)}, opportunity_snapshots={"BTC": opportunity("BTC")}, external_evidence={"BTC": external("BTC")}, observation_number=50)
        btc = next(r for r in updated["records"] if r["symbol"] == "BTC")
        self.assertEqual(btc["outcomes"]["12H"]["horizon_status"], "PENDING")

    def test_twenty_four_hour_stays_pending_if_not_ready(self) -> None:
        store = backfill_46_49("fixed")
        updated, _ = dry_run_missed_opportunity_observation(store=store, reviews={"ETH": terminal_review("ETH")}, frames={"ETH": frames("ETH", count=60)}, opportunity_snapshots={"ETH": opportunity("ETH")}, external_evidence={"ETH": external("ETH")}, observation_number=50)
        eth = next(r for r in updated["records"] if r["symbol"] == "ETH")
        self.assertEqual(eth["outcomes"]["24H"]["horizon_status"], "PENDING")

    def test_data_gap_outcome_handling(self) -> None:
        store = backfill_46_49("fixed")
        gap_frame = frame("BTC", start=1787835000, count=1, latest_closed=1787835000)
        updated, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": terminal_review("BTC")}, frames={"BTC": {tf: gap_frame for tf in ("D1", "H4", "H1", "M15", "M5")}}, opportunity_snapshots={"BTC": opportunity("BTC")}, external_evidence={"BTC": external("BTC")}, observation_number=50)
        btc = next(r for r in updated["records"] if r["symbol"] == "BTC")
        self.assertIn(btc["outcomes"]["1H"]["horizon_status"], {"DATA_GAP", "PENDING"})

    def test_no_open_candle_mfe_mae(self) -> None:
        store = backfill_46_49("fixed")
        m5 = frame("BTC", count=12, price=80000, latest_closed=1787831400)
        m5.candles.append(candle(1787831700, 90000, high=99999, low=1))
        updated, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": terminal_review("BTC")}, frames={"BTC": {tf: m5 for tf in ("D1", "H4", "H1", "M15", "M5")}}, opportunity_snapshots={"BTC": opportunity("BTC")}, external_evidence={"BTC": external("BTC")}, observation_number=50)
        btc = next(r for r in updated["records"] if r["symbol"] == "BTC")
        if btc["outcomes"]["1H"]["MFE_price"] is not None:
            self.assertLess(btc["outcomes"]["1H"]["MFE_price"], 99999)

    def test_decision_evidence_no_hindsight(self) -> None:
        store = backfill_46_49("fixed")
        before = copy.deepcopy(store["records"][0]["snapshots"][0])
        dry_run_missed_opportunity_observation(store=store, reviews={"BTC": terminal_review("BTC")}, frames={"BTC": frames("BTC")}, opportunity_snapshots={"BTC": opportunity("BTC")}, external_evidence={"BTC": external("BTC")}, observation_number=50)
        self.assertEqual(store["records"][0]["snapshots"][0], before)

    def test_production_conversion_detection(self) -> None:
        store = backfill_46_49("fixed")
        updated, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": genesis_review("BTC")}, frames={"BTC": frames("BTC")}, opportunity_snapshots={"BTC": opportunity("BTC", sequence_state="SEEKING_LIQUIDITY")}, external_evidence={"BTC": external("BTC")}, observation_number=50)
        btc = next(r for r in updated["records"] if r["symbol"] == "BTC")
        self.assertTrue(btc["converted_to_production"])
        self.assertEqual(btc["production_sequence_id"], "BTC-seq-0010")

    def test_no_false_conversion_from_candidate_draw(self) -> None:
        store = backfill_46_49("fixed")
        review = terminal_review("BTC")
        review["Candidate_Tactical_Draw"] = "Internal Sell-side Liquidity 1 on H1, formed_at=1"
        updated, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": review}, frames={"BTC": frames("BTC")}, opportunity_snapshots={"BTC": opportunity("BTC")}, external_evidence={"BTC": external("BTC")}, observation_number=50)
        btc = next(r for r in updated["records"] if r["symbol"] == "BTC")
        self.assertFalse(btc["converted_to_production"])

    def test_converted_tracker_remains_research_only(self) -> None:
        store = backfill_46_49("fixed")
        updated, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": genesis_review("BTC")}, frames={"BTC": frames("BTC")}, opportunity_snapshots={"BTC": opportunity("BTC", sequence_state="SEEKING_LIQUIDITY")}, external_evidence={"BTC": external("BTC")}, observation_number=50)
        payload = json.dumps(updated).lower()
        self.assertNotIn('"entry"', payload)
        self.assertNotIn('"score"', payload)

    def test_terminal_tracker_can_finish_pending_horizons(self) -> None:
        store = backfill_46_49("fixed")
        store["records"][0]["status"] = "TERMINAL"
        updated, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": terminal_review("BTC")}, frames={"BTC": frames("BTC", count=60)}, opportunity_snapshots={"BTC": opportunity("BTC")}, external_evidence={"BTC": external("BTC")}, observation_number=50)
        btc = next(r for r in updated["records"] if r["symbol"] == "BTC")
        self.assertIn(btc["outcomes"]["1H"]["horizon_status"], {"COMPLETE", "DATA_GAP", "PENDING"})

    def test_restart_reload_continues_same_tracker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            explicit_backfill_v426(path)
            first = missed_opportunity_status(path)["records"][0]["tracker_id"]
            apply_once(path)
            second = missed_opportunity_status(path)["records"][0]["tracker_id"]
            self.assertEqual(first, second)

    def test_atomic_research_persist_and_fresh_reload(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            report = apply_once(path)
            self.assertEqual(report["fresh_reload_match"], True)
            self.assertTrue(path.exists())

    def test_relation_features_preserved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            apply_once(path)
            latest = load_tracker_store(path)["records"][0]["snapshots"][-1]
            self.assertIn("ZERO_LIQUIDATION_CONTEXT", latest["relation_features"])
            self.assertNotIn("PRICE_OI_BUILD", latest["relation_features"])

    def test_tracker_health_report(self) -> None:
        health = tracker_health(backfill_46_49("fixed"))
        self.assertIn("active_records", health)
        self.assertIn("pending_outcomes", health)

    def test_status_cli_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            explicit_backfill_v426(path)
            before = path.read_text(encoding="utf-8")
            result = main(["missed-opportunity-status", "--path", str(path)])
            self.assertEqual(result, 0)
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_backfill_cli(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            result = main(["backfill-missed-opportunities-v426", "--path", str(path)])
            self.assertEqual(result, 0)
            self.assertEqual(load_tracker_store(path)["records"].__len__(), 2)

    def test_no_score_grade(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            apply_once(path)
            payload = path.read_text(encoding="utf-8").lower()
            self.assertNotIn('"score"', payload)
            self.assertNotIn('"grade"', payload)

    def test_no_entry_sl_tp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            apply_once(path)
            payload = path.read_text(encoding="utf-8").lower()
            self.assertNotIn('"entry"', payload)
            self.assertNotIn('"sl"', payload)
            self.assertNotIn('"tp"', payload)

    def test_production_lifecycle_unchanged(self) -> None:
        review = terminal_review("BTC")
        before = copy.deepcopy(review)
        with tempfile.TemporaryDirectory() as tmp:
            apply_once(Path(tmp) / "research" / "missed-opportunities.json", review=review)
        self.assertEqual(review, before)

    def test_external_tracker_fields_preserve_current_metrics(self) -> None:
        fields = external_tracker_fields(external("BTC"))
        self.assertEqual(fields["oi_delta_5m"], 5)
        self.assertEqual(fields["liquidation_5m"]["long"], 0)

    def test_opportunity_evidence_uses_snapshot_and_external_domains(self) -> None:
        ev = opportunity_evidence_from_snapshot(opportunity("BTC"), external("BTC"))
        self.assertEqual(ev["STRUCTURE"], "POSITIVE")
        self.assertEqual(ev["MOMENTUM"], "POSITIVE")
        self.assertEqual(ev["POSITIONING"], "POSITIVE")


    def test_reentry_after_explicit_break_starts_new_tracker(self) -> None:
        store = empty_store("fixed")
        good70 = opportunity("BTC")
        good70["snapshot_timestamp"] = SNAPSHOT_TS + 70
        store, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": terminal_review("BTC")}, frames={"BTC": frames("BTC", price=100)}, opportunity_snapshots={"BTC": good70}, external_evidence={"BTC": external("BTC", SNAPSHOT_TS + 70)}, observation_number=70)
        bad = opportunity("BTC", htf=0, momentum="MATURE_CONTINUATION")
        bad["snapshot_timestamp"] = SNAPSHOT_TS + 72
        store, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": terminal_review("BTC")}, frames={"BTC": frames("BTC", price=101)}, opportunity_snapshots={"BTC": bad}, external_evidence={"BTC": external("BTC", SNAPSHOT_TS + 72)}, observation_number=72)
        good74 = opportunity("BTC")
        good74["snapshot_timestamp"] = SNAPSHOT_TS + 74
        store, report = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": terminal_review("BTC")}, frames={"BTC": frames("BTC", price=102)}, opportunity_snapshots={"BTC": good74}, external_evidence={"BTC": external("BTC", SNAPSHOT_TS + 74)}, observation_number=74)
        btc_records = [r for r in store["records"] if r["symbol"] == "BTC"]
        self.assertEqual(len(btc_records), 2)
        self.assertNotEqual(btc_records[0]["tracker_id"], btc_records[1]["tracker_id"])
        self.assertEqual([s["observation_number"] for s in btc_records[0]["snapshots"]], [70, 72])
        self.assertEqual(btc_records[0]["episode_status"], "BROKEN")
        self.assertEqual(btc_records[0]["context_break_observation"], 72)
        self.assertEqual(btc_records[1]["origin_observation"], 74)
        self.assertEqual(report["symbols"]["BTC"]["tracker_id"], btc_records[1]["tracker_id"])

    def test_active_tracker_does_not_split_on_core_context_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            apply_once(path)
            original_id = load_tracker_store(path)["records"][0]["tracker_id"]
            opp = opportunity("BTC")
            opp["sequence_id"] = "BTC-seq-0011"
            opp["features"]["REGIME"]["value"]["regime"] = "NEW_DELIVERY_LEG"
            review = terminal_review("BTC")
            review["Sequence_ID"] = "BTC-seq-0011"
            apply_missed_opportunity_observation(reviews={"BTC": review}, frames={"BTC": frames("BTC", price=101)}, opportunity_snapshots={"BTC": opp}, external_evidence={"BTC": external("BTC")}, observation_number=51, store_path=path, persist=True)
            store = load_tracker_store(path)
            self.assertEqual(len(store["records"]), 1)
            self.assertEqual(store["records"][0]["tracker_id"], original_id)
            self.assertEqual(len(store["records"][0]["snapshots"]), 2)

    def test_price_context_from_frames_uses_matching_oi_horizons(self) -> None:
        fr = frames("BTC", price=100, count=60)
        context = price_change_context_from_frames(fr, fr["M5"].latest_closed_candle_timestamp)
        self.assertIn("price_change_5m_pct", context)
        self.assertIn("price_change_1h_pct", context)

    def test_live_enrichment_classifies_price_down_oi_up_prospectively(self) -> None:
        store = backfill_46_49("fixed")
        fr = frame("BTC", start=SNAPSHOT_TS, count=20, price=100)
        fr.candles = [candle(SNAPSHOT_TS + index * 300, 120 - index) for index in range(20)]
        fr.latest_closed_candle_timestamp = fr.candles[-1].timestamp
        fr.latest_candle_timestamp = fr.candles[-1].timestamp
        all_frames = {tf: fr for tf in ("D1", "H4", "H1", "M15", "M5")}
        ext = build_external_market_evidence("BTC", fr.latest_closed_candle_timestamp, {
            "oi_change_1h": metric("oi_change_1h", 10, "fixture", fr.latest_closed_candle_timestamp, fr.latest_closed_candle_timestamp, "1h"),
            "long_liquidation_notional_5m": metric("long_liquidation_notional_5m", 0, "fixture", fr.latest_closed_candle_timestamp, fr.latest_closed_candle_timestamp, "5m"),
            "short_liquidation_notional_5m": metric("short_liquidation_notional_5m", 0, "fixture", fr.latest_closed_candle_timestamp, fr.latest_closed_candle_timestamp, "5m"),
        })
        updated, _ = dry_run_missed_opportunity_observation(store=store, reviews={"BTC": terminal_review("BTC")}, frames={"BTC": all_frames}, opportunity_snapshots={"BTC": opportunity("BTC")}, external_evidence={"BTC": ext}, observation_number=50)
        btc = next(r for r in updated["records"] if r["symbol"] == "BTC")
        self.assertIn("PRICE_DOWN_OI_UP", btc["snapshots"][-1]["relation_features"])
        self.assertIn("ZERO_LIQUIDATION_CONTEXT", btc["snapshots"][-1]["relation_features"])
        self.assertNotIn("PRICE_OI_BUILD", btc["snapshots"][-1]["relation_features"])

if __name__ == "__main__":
    unittest.main()
