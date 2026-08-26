from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer import liquidation_collector as collector_module
from market_reviewer.external_evidence import build_external_market_evidence
from market_reviewer.liquidation_collector import (
    COVERAGE_COMPLETE,
    COVERAGE_PARTIAL,
    COVERAGE_UNAVAILABLE,
    CoverageStore,
    LiquidationEventStore,
    RawLiquidationEvent,
    aggregate_liquidations,
    binance_force_order_side_to_liquidation,
    liquidation_metrics_from_store,
    liquidation_status,
    parse_binance_force_order,
)
from market_reviewer.persistence import atomic_write_json
from market_reviewer.pipeline import review_snapshot
from tests.test_opportunity_v42 import REVIEW29, review28_state


SNAPSHOT_TS = 1_800_000_000


def payload(side: str = "SELL", price: str = "100", qty: str = "2", event_ms: int | None = None, symbol: str = "BTCUSDT", trade_id: str = "1") -> dict:
    event_ms = event_ms if event_ms is not None else (SNAPSHOT_TS - 60) * 1000
    return {
        "e": "forceOrder",
        "E": event_ms,
        "o": {
            "s": symbol,
            "S": side,
            "o": "LIMIT",
            "f": "IOC",
            "q": qty,
            "p": price,
            "ap": price,
            "X": "FILLED",
            "l": trade_id,
            "z": qty,
            "T": event_ms,
        },
    }


def complete_coverage(symbol: str = "BTC", started: int = SNAPSHOT_TS - 4000, last: int = SNAPSHOT_TS) -> dict:
    return {
        "store_version": "liquidation-event-store.v1",
        "symbol": symbol,
        "collector_started_at": started,
        "last_event_received_at": last,
        "last_stream_message_at": last,
        "disconnect_intervals": [],
        "reconnect_intervals": [],
        "coverage_status": COVERAGE_COMPLETE,
    }


class LiquidationCollectorV425Tests(unittest.TestCase):
    def event(self, side: str = "LONG_LIQUIDATION", ts: int = SNAPSHOT_TS - 60, received: int = SNAPSHOT_TS - 30, symbol: str = "BTC", price: float = 100.0, qty: float = 2.0) -> RawLiquidationEvent:
        return RawLiquidationEvent(
            symbol=symbol,
            provider="binance_usdm_futures",
            market_type="usdt_perpetual",
            event_id=f"{symbol}-{side}-{ts}",
            side=side,
            price=price,
            quantity=qty,
            notional_usd=price * qty,
            event_timestamp=ts,
            received_timestamp=received,
            available_at=received,
            raw={"fixture": True},
            fingerprint=f"{symbol}-{side}-{ts}-{price}-{qty}",
        )

    def test_long_liquidation_side_mapping(self) -> None:
        self.assertEqual(binance_force_order_side_to_liquidation("SELL"), "LONG_LIQUIDATION")
        event = parse_binance_force_order(payload("SELL"), SNAPSHOT_TS)
        self.assertEqual(event.side, "LONG_LIQUIDATION")

    def test_short_liquidation_side_mapping(self) -> None:
        self.assertEqual(binance_force_order_side_to_liquidation("BUY"), "SHORT_LIQUIDATION")
        event = parse_binance_force_order(payload("BUY"), SNAPSHOT_TS)
        self.assertEqual(event.side, "SHORT_LIQUIDATION")

    def test_event_append(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LiquidationEventStore(Path(directory))
            self.assertTrue(store.append(self.event()))
            self.assertEqual(store.event_count("BTC"), 1)

    def test_deterministic_dedupe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LiquidationEventStore(Path(directory))
            event = self.event()
            self.assertTrue(store.append(event))
            self.assertFalse(store.append(event))
            self.assertEqual(store.event_count("BTC"), 1)

    def test_duplicate_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event = self.event()
            self.assertTrue(LiquidationEventStore(root).append(event))
            self.assertFalse(LiquidationEventStore(root).append(event))
            self.assertEqual(LiquidationEventStore(root).event_count("BTC"), 1)

    def test_btc_eth_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LiquidationEventStore(Path(directory))
            store.append(self.event(symbol="BTC"))
            store.append(self.event(symbol="ETH"))
            self.assertEqual(store.event_count("BTC"), 1)
            self.assertEqual(store.event_count("ETH"), 1)

    def test_5m_aggregation(self) -> None:
        result = aggregate_liquidations([self.event()], complete_coverage(), SNAPSHOT_TS)
        self.assertEqual(result["5m"]["long_liquidation_notional"], 200.0)
        self.assertEqual(result["5m"]["event_count_long"], 1)

    def test_15m_aggregation(self) -> None:
        result = aggregate_liquidations([self.event(ts=SNAPSHOT_TS - 700)], complete_coverage(), SNAPSHOT_TS)
        self.assertEqual(result["15m"]["long_liquidation_notional"], 200.0)
        self.assertEqual(result["5m"]["long_liquidation_notional"], 0)

    def test_1h_aggregation(self) -> None:
        result = aggregate_liquidations([self.event(side="SHORT_LIQUIDATION", ts=SNAPSHOT_TS - 3000)], complete_coverage(), SNAPSHOT_TS)
        self.assertEqual(result["1h"]["short_liquidation_notional"], 200.0)
        self.assertEqual(result["15m"]["short_liquidation_notional"], 0)

    def test_complete_zero_event_window_is_available_zero(self) -> None:
        result = aggregate_liquidations([], complete_coverage(), SNAPSHOT_TS)
        self.assertEqual(result["5m"]["coverage_status"], COVERAGE_COMPLETE)
        self.assertEqual(result["5m"]["long_liquidation_notional"], 0)
        self.assertEqual(result["5m"]["short_liquidation_notional"], 0)

    def test_partial_zero_event_window_is_not_zero_evidence(self) -> None:
        coverage = complete_coverage(started=SNAPSHOT_TS - 100, last=SNAPSHOT_TS)
        result = aggregate_liquidations([], coverage, SNAPSHOT_TS)
        self.assertEqual(result["5m"]["coverage_status"], COVERAGE_PARTIAL)
        self.assertIsNone(result["5m"]["long_liquidation_notional"])

    def test_unavailable_coverage_is_data_unavailable(self) -> None:
        result = aggregate_liquidations([], {"coverage_status": COVERAGE_UNAVAILABLE}, SNAPSHOT_TS)
        self.assertEqual(result["5m"]["coverage_status"], COVERAGE_UNAVAILABLE)
        self.assertIsNone(result["5m"]["total_liquidation_notional"])

    def test_disconnect_creates_coverage_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coverage = CoverageStore(Path(directory))
            coverage.mark_started("BTC", SNAPSHOT_TS - 1000)
            value = coverage.mark_disconnect("BTC", SNAPSHOT_TS - 100, "TEST")
            self.assertEqual(value["coverage_status"], COVERAGE_PARTIAL)
            self.assertEqual(value["disconnect_intervals"][0]["reason"], "TEST")

    def test_reconnect_resumes_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coverage = CoverageStore(Path(directory))
            coverage.mark_started("BTC", SNAPSHOT_TS - 1000)
            coverage.mark_disconnect("BTC", SNAPSHOT_TS - 100, "TEST")
            value = coverage.mark_reconnect("BTC", SNAPSHOT_TS)
            self.assertEqual(value["coverage_status"], COVERAGE_COMPLETE)
            self.assertEqual(value["disconnect_intervals"][0]["end"], SNAPSHOT_TS)

    def test_late_arriving_event_no_hindsight(self) -> None:
        event = self.event(ts=SNAPSHOT_TS - 60, received=SNAPSHOT_TS + 1)
        result = aggregate_liquidations([event], complete_coverage(), SNAPSHOT_TS)
        self.assertEqual(result["5m"]["long_liquidation_notional"], 0)

    def test_event_timestamp_and_received_timestamp_preserved(self) -> None:
        event = parse_binance_force_order(payload("SELL", event_ms=(SNAPSHOT_TS - 60) * 1000), SNAPSHOT_TS)
        self.assertEqual(event.event_timestamp, SNAPSHOT_TS - 60)
        self.assertEqual(event.received_timestamp, SNAPSHOT_TS)
        self.assertEqual(event.available_at, SNAPSHOT_TS)

    def test_external_evidence_integration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = LiquidationEventStore(root)
            coverage = CoverageStore(root)
            coverage.save("BTC", complete_coverage())
            store.append(self.event())
            metrics = liquidation_metrics_from_store(store, coverage, "BTC", SNAPSHOT_TS)
            evidence = build_external_market_evidence("BTC", SNAPSHOT_TS, metrics)
        self.assertEqual(evidence["raw_metrics"]["long_liquidation_notional_5m"]["value"], 200.0)
        self.assertEqual(evidence["relation_features"]["LIQUIDATION_CONTEXT_RELATION"]["value"]["relation"], "LONG_LIQUIDATION_FLUSH")

    def test_unavailable_collector_does_not_block_production(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            atomic_write_json(state_path, review28_state())
            status = liquidation_status(Path(directory) / "liquidations")
            reviews = review_snapshot(REVIEW29, state_path)
        self.assertEqual(status[0]["connected"], False)
        self.assertIn("BTC", reviews)

    def test_raw_store_append_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = LiquidationEventStore(Path(directory))
            first = self.event(ts=SNAPSHOT_TS - 60)
            second = self.event(ts=SNAPSHOT_TS - 30)
            store.append(first)
            before = store.events_path("BTC").read_text(encoding="utf-8")
            store.append(second)
            after = store.events_path("BTC").read_text(encoding="utf-8")
        self.assertTrue(after.startswith(before))
        self.assertEqual(len([line for line in after.splitlines() if line.strip()]), 2)

    def test_restart_safety(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = LiquidationEventStore(root)
            coverage = CoverageStore(root)
            coverage.mark_started("BTC", SNAPSHOT_TS - 4000)
            coverage.mark_message("BTC", SNAPSHOT_TS)
            self.assertTrue(store.append(self.event()))
            reloaded_store = LiquidationEventStore(root)
            reloaded_coverage = CoverageStore(root)
            metrics = liquidation_metrics_from_store(reloaded_store, reloaded_coverage, "BTC", SNAPSHOT_TS)
            self.assertEqual(reloaded_store.event_count("BTC"), 1)
            self.assertEqual(metrics["long_liquidation_notional_5m"]["value"], 200.0)


    def transport_coverage(self, start: int, end: int, symbol: str = "BTC", last_message: int | None = None) -> dict:
        coverage = complete_coverage(symbol=symbol, started=start, last=last_message or start)
        coverage["collector_stopped_at"] = end
        coverage["last_transport_alive_at"] = end
        coverage["coverage_intervals"] = [{"start": start, "end": end, "status": "CONNECTED"}]
        return coverage

    def test_quiet_connected_5m_window_is_complete_zero(self) -> None:
        coverage = self.transport_coverage(SNAPSHOT_TS - 600, SNAPSHOT_TS)
        result = aggregate_liquidations([], coverage, SNAPSHOT_TS)
        self.assertEqual(result["5m"]["coverage_status"], COVERAGE_COMPLETE)
        self.assertEqual(result["5m"]["long_liquidation_notional"], 0)
        self.assertEqual(result["5m"]["short_liquidation_notional"], 0)

    def test_quiet_connected_15m_window_is_complete_zero(self) -> None:
        coverage = self.transport_coverage(SNAPSHOT_TS - 1200, SNAPSHOT_TS)
        result = aggregate_liquidations([], coverage, SNAPSHOT_TS)
        self.assertEqual(result["15m"]["coverage_status"], COVERAGE_COMPLETE)
        self.assertEqual(result["15m"]["total_liquidation_notional"], 0)

    def test_quiet_connected_1h_window_is_complete_zero(self) -> None:
        coverage = self.transport_coverage(SNAPSHOT_TS - 4000, SNAPSHOT_TS)
        result = aggregate_liquidations([], coverage, SNAPSHOT_TS)
        self.assertEqual(result["1h"]["coverage_status"], COVERAGE_COMPLETE)
        self.assertEqual(result["1h"]["total_liquidation_notional"], 0)

    def test_fully_before_collector_start_is_unavailable(self) -> None:
        coverage = self.transport_coverage(SNAPSHOT_TS, SNAPSHOT_TS + 600)
        result = aggregate_liquidations([], coverage, SNAPSHOT_TS - 1)
        self.assertEqual(result["5m"]["coverage_status"], COVERAGE_UNAVAILABLE)

    def test_half_before_start_is_partial(self) -> None:
        coverage = self.transport_coverage(SNAPSHOT_TS - 150, SNAPSHOT_TS + 600)
        result = aggregate_liquidations([], coverage, SNAPSHOT_TS)
        self.assertEqual(result["5m"]["coverage_status"], COVERAGE_PARTIAL)

    def test_disconnect_overlap_window_is_partial(self) -> None:
        coverage = self.transport_coverage(SNAPSHOT_TS - 600, SNAPSHOT_TS)
        coverage["coverage_intervals"] = [
            {"start": SNAPSHOT_TS - 600, "end": SNAPSHOT_TS - 200, "status": "CONNECTED"},
            {"start": SNAPSHOT_TS - 100, "end": SNAPSHOT_TS, "status": "CONNECTED"},
        ]
        coverage["disconnect_intervals"] = [{"start": SNAPSHOT_TS - 200, "end": SNAPSHOT_TS - 100, "reason": "TEST"}]
        result = aggregate_liquidations([], coverage, SNAPSHOT_TS)
        self.assertEqual(result["5m"]["coverage_status"], COVERAGE_PARTIAL)

    def test_old_last_stream_message_with_current_heartbeat_is_complete(self) -> None:
        coverage = self.transport_coverage(SNAPSHOT_TS - 600, SNAPSHOT_TS, last_message=SNAPSHOT_TS - 600)
        result = aggregate_liquidations([], coverage, SNAPSHOT_TS)
        self.assertEqual(result["5m"]["coverage_status"], COVERAGE_COMPLETE)
        self.assertEqual(result["5m"]["total_liquidation_notional"], 0)

    def test_clean_shutdown_closes_coverage_interval_and_status_false(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coverage = CoverageStore(root)
            coverage.mark_started("BTC", SNAPSHOT_TS - 100)
            coverage.mark_transport_alive("BTC", SNAPSHOT_TS)
            stopped = coverage.mark_stopped("BTC", SNAPSHOT_TS + 1)
            status = liquidation_status(root, symbols=("BTC",))[0]
        self.assertEqual(stopped["coverage_intervals"][-1]["end"], SNAPSHOT_TS + 1)
        self.assertFalse(status["connected"])

    def test_collect_clean_shutdown_top_level_connected_false(self) -> None:
        original = collector_module._read_websocket_json
        try:
            collector_module._read_websocket_json = lambda _url, _duration: iter([None])
            with tempfile.TemporaryDirectory() as directory:
                result = collector_module.collect_liquidations(Path(directory), duration_seconds=1, symbols=("BTC",))
        finally:
            collector_module._read_websocket_json = original
        self.assertFalse(result["connected"])
        self.assertTrue(result["completed"])
        self.assertFalse(result["status"][0]["connected"])
    def test_reconnect_creates_second_transport_interval(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coverage = CoverageStore(Path(directory))
            coverage.mark_started("BTC", SNAPSHOT_TS - 1000)
            coverage.mark_disconnect("BTC", SNAPSHOT_TS - 500, "TEST")
            value = coverage.mark_reconnect("BTC", SNAPSHOT_TS - 100)
        self.assertEqual(len(value["coverage_intervals"]), 2)
        self.assertEqual(value["coverage_intervals"][0]["end"], SNAPSHOT_TS - 500)
        self.assertIsNone(value["coverage_intervals"][1]["end"])

    def test_restart_does_not_bridge_unknown_gap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            coverage = CoverageStore(Path(directory))
            coverage.mark_started("BTC", 1000)
            coverage.mark_transport_alive("BTC", 1100)
            coverage.mark_started("BTC", 2000)
            loaded = coverage.load("BTC")
        self.assertEqual(loaded["coverage_intervals"][0], {"start": 1000, "end": 1100, "status": "CONNECTED"})
        self.assertEqual(loaded["coverage_intervals"][1], {"start": 2000, "end": None, "status": "CONNECTED"})
        result = aggregate_liquidations([], loaded, 1500, windows=(300,))
        self.assertEqual(result["5m"]["coverage_status"], COVERAGE_UNAVAILABLE)

    def test_zero_complete_metrics_available_but_partial_metrics_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = LiquidationEventStore(root)
            coverage = CoverageStore(root)
            coverage.save("BTC", self.transport_coverage(SNAPSHOT_TS - 600, SNAPSHOT_TS))
            complete_metrics = liquidation_metrics_from_store(store, coverage, "BTC", SNAPSHOT_TS)
            coverage.save("BTC", self.transport_coverage(SNAPSHOT_TS - 100, SNAPSHOT_TS))
            partial_metrics = liquidation_metrics_from_store(store, coverage, "BTC", SNAPSHOT_TS)
        self.assertEqual(complete_metrics["long_liquidation_notional_5m"]["availability"], "AVAILABLE")
        self.assertEqual(complete_metrics["long_liquidation_notional_5m"]["value"], 0)
        self.assertEqual(partial_metrics["long_liquidation_notional_5m"]["availability"], "PARTIAL")
        self.assertIsNone(partial_metrics["long_liquidation_notional_5m"]["value"])
if __name__ == "__main__":
    unittest.main()
