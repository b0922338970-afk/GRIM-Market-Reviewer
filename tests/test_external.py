from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer.external import (
    DEFAULT_BOOTSTRAP_CANDLES,
    FETCH_MODE_BOOTSTRAP,
    FETCH_MODE_PRODUCTION_REPLAY,
    FetchDepthPlan,
    ReplayHistoryTooOld,
    ReplayStateUnavailableForFetch,
    build_fetch_depth_plan,
    build_symbol_generation,
    fetch_depth_plan_for_mode,
    fetch_external_generation,
    fetch_with_depth_plan,
    has_new_closed_candle,
    latest_closed_index,
    load_required_replay_state,
    publish_artifact,
)
from market_reviewer.missed_opportunity import backfill_46_49, persist_tracker_store
from market_reviewer.model import Candle, DataUnavailable, TIMEFRAMES, TIMEFRAME_SECONDS, to_market_data_frame
from market_reviewer.providers import choose_complete_provider


def write_replay_state(path: Path, btc_timestamp: int | None = 1_700_000_000, eth_timestamp: int | None = 1_700_000_000) -> None:
    symbols = {}
    if btc_timestamp is not None:
        symbols["BTC"] = {"previous_review_timestamp": btc_timestamp}
    if eth_timestamp is not None:
        symbols["ETH"] = {"previous_review_timestamp": eth_timestamp}
    path.write_text(
        json.dumps({"persistence_version": 2, "state_schema": "review-state.v2", "symbols": symbols}),
        encoding="utf-8",
    )


class FakeProvider:
    def __init__(self, name: str, missing: set[str] | None = None) -> None:
        self.name = name
        self.missing = missing or set()

    def fetch_ohlcv(self, symbol: str, timeframe: str) -> list[Candle]:
        if timeframe in self.missing:
            return []
        duration = {"D1": 86400, "H4": 14400, "H1": 3600, "M15": 900, "M5": 300}[timeframe]
        base = 1_700_000_000
        return [
            Candle(base + i * duration, 100 + i, 102 + i, 99 + i, 101 + i, 10)
            for i in range(55)
        ]


def closed_fetch_timestamp(raw: dict[str, list[Candle]]) -> int:
    return max(candles[-1].timestamp + {"D1": 86400, "H4": 14400, "H1": 3600, "M15": 900, "M5": 300}[tf] for tf, candles in raw.items())


class ExternalTests(unittest.TestCase):
    def test_provider_fallback_uses_single_complete_provider(self) -> None:
        provider, frames = choose_complete_provider(
            "BTC",
            [FakeProvider("bitunix_perpetual", {"M5"}), FakeProvider("binance")],
        )
        self.assertEqual(provider.name, "binance")
        self.assertEqual(set(frames), set(TIMEFRAMES))

    def test_open_candle_is_not_latest_closed(self) -> None:
        provider = FakeProvider("binance")
        raw = {tf: provider.fetch_ohlcv("BTC", tf) for tf in TIMEFRAMES}
        fetch_timestamp = raw["M5"][-1].timestamp + 1
        generation = build_symbol_generation("BTC", provider, raw, fetch_timestamp)
        self.assertEqual(
            generation["M5"]["current_open_candle_timestamp"],
            raw["M5"][-1].timestamp,
        )
        self.assertEqual(
            generation["M5"]["latest_closed_candle_timestamp"],
            raw["M5"][-2].timestamp,
        )

    def test_publish_validates_before_writing_artifact(self) -> None:
        provider = FakeProvider("binance")
        raw = {tf: provider.fetch_ohlcv("ETH", tf) for tf in TIMEFRAMES}
        generation = {"ETH": build_symbol_generation("ETH", provider, raw, closed_fetch_timestamp(raw))}
        with tempfile.TemporaryDirectory() as directory:
            path = publish_artifact(generation, Path(directory))
            self.assertTrue(path.exists())

    def test_generation_rejects_too_few_closed_candles(self) -> None:
        provider = FakeProvider("binance")
        raw = {tf: provider.fetch_ohlcv("BTC", tf)[:10] for tf in TIMEFRAMES}
        generation = build_symbol_generation("BTC", provider, raw, closed_fetch_timestamp(raw))
        frame = to_market_data_frame(generation["M5"])
        with self.assertRaises(DataUnavailable):
            from market_reviewer.model import validate_market_data_frame

            validate_market_data_frame(frame)

    def test_no_change_gate_detects_unchanged_closed_candles(self) -> None:
        provider = FakeProvider("binance")
        raw = {tf: provider.fetch_ohlcv("BTC", tf) for tf in TIMEFRAMES}
        snapshot = {"BTC": build_symbol_generation("BTC", provider, raw, closed_fetch_timestamp(raw))}
        index = latest_closed_index(snapshot)
        self.assertFalse(has_new_closed_candle(index, index))
        self.assertTrue(has_new_closed_candle(None, index))


class PaginatedProvider:
    def __init__(self, name: str = "binance", fail_timeframes: set[str] | None = None, gap: bool = False) -> None:
        self.name = name
        self.fail_timeframes = fail_timeframes or set()
        self.gap = gap
        self.calls: list[tuple[str, str, int, int | None]] = []

    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 200, end_timestamp: int | None = None) -> list[Candle]:
        self.calls.append((symbol, timeframe, limit, end_timestamp))
        if timeframe in self.fail_timeframes:
            return []
        duration = TIMEFRAME_SECONDS[timeframe]
        end = 1_700_200_000 if end_timestamp is None else end_timestamp
        last_open = end - (end % duration) - duration
        candles = []
        for index in range(limit):
            ts = last_open - (limit - index - 1) * duration
            if self.gap and index == limit // 2:
                ts += duration
            candles.append(Candle(ts, 100 + index, 102 + index, 99 + index, 101 + index, 10))
        return candles


class DynamicFetchDepthTests(unittest.TestCase):
    def test_short_gap_uses_bootstrap_minimum(self) -> None:
        fetch_timestamp = 1_700_000_000 + 3600
        plan = build_fetch_depth_plan("M5", fetch_timestamp, 1_700_000_000)
        self.assertEqual(plan.requested_bars, DEFAULT_BOOTSTRAP_CANDLES)
        self.assertLess(plan.replay_required_bars, DEFAULT_BOOTSTRAP_CANDLES)

    def test_two_day_gap_increases_m5_depth(self) -> None:
        previous = 1_700_000_000
        fetch_timestamp = previous + 2 * 86_400
        plan = build_fetch_depth_plan("M5", fetch_timestamp, previous)
        self.assertGreater(plan.replay_required_bars, DEFAULT_BOOTSTRAP_CANDLES)
        self.assertGreater(plan.requested_bars, plan.replay_required_bars)

    def test_per_timeframe_requested_depth_differs(self) -> None:
        previous = 1_700_000_000
        fetch_timestamp = previous + 2 * 86_400
        m5 = build_fetch_depth_plan("M5", fetch_timestamp, previous)
        h1 = build_fetch_depth_plan("H1", fetch_timestamp, previous)
        self.assertGreater(m5.requested_bars, h1.requested_bars)

    def test_pagination_when_provider_limit_exceeded(self) -> None:
        provider = PaginatedProvider("bitunix_perpetual")
        plan = FetchDepthPlan("M5", 1_700_000_000, 1_700_300_000, 1000, 50, 10, 1100, 1_700_000_300)
        candles, diagnostic = fetch_with_depth_plan(provider, "BTC", "M5", plan)
        self.assertGreater(diagnostic["pagination_pages"], 1)
        self.assertLessEqual(candles[0].timestamp, plan.coverage_start_timestamp)

    def test_page_merge_has_no_duplicate_timestamps(self) -> None:
        provider = PaginatedProvider("bitunix_perpetual")
        plan = FetchDepthPlan("M5", 1_700_000_000, 1_700_300_000, 650, 50, 10, 410, 1_700_080_000)
        candles, _ = fetch_with_depth_plan(provider, "BTC", "M5", plan)
        timestamps = [c.timestamp for c in candles]
        self.assertEqual(len(timestamps), len(set(timestamps)))

    def test_page_merge_rejects_timestamp_gaps(self) -> None:
        provider = PaginatedProvider("binance", gap=True)
        plan = FetchDepthPlan("M5", 1_700_000_000, 1_700_100_000, 250, 50, 10, 260, None)
        candles, diagnostic = fetch_with_depth_plan(provider, "BTC", "M5", plan)
        self.assertEqual(candles, [])
        self.assertEqual(diagnostic["pagination_error"], "TIMESTAMP_GAP")

    def test_same_provider_gate_preserved(self) -> None:
        first = PaginatedProvider("bitunix_perpetual", {"M5"})
        second = PaginatedProvider("binance")
        snapshot = fetch_external_generation([first, second], 1_700_200_000)
        providers = {frame["provider"] for frame in snapshot["BTC"].values()}
        self.assertEqual(providers, {"binance"})

    def test_provider_pagination_failure_falls_back_whole_provider(self) -> None:
        first = PaginatedProvider("bitunix_perpetual", {"M15"})
        second = PaginatedProvider("binance")
        snapshot = fetch_external_generation([first, second], 1_700_200_000)
        providers = {frame["provider"] for frame in snapshot["ETH"].values()}
        self.assertEqual(providers, {"binance"})

    def test_enough_replay_bars_still_requests_context(self) -> None:
        plan = build_fetch_depth_plan("M5", 1_700_006_000, 1_700_000_000)
        self.assertGreaterEqual(plan.context_required_bars, 50)
        self.assertGreater(plan.requested_bars, plan.replay_required_bars)

    def test_no_persisted_state_uses_bootstrap_behavior(self) -> None:
        plan = build_fetch_depth_plan("M5", 1_700_200_000, None)
        self.assertEqual(plan.requested_bars, DEFAULT_BOOTSTRAP_CANDLES)
        self.assertEqual(plan.replay_required_bars, 0)

    def test_excessive_gap_returns_history_too_old(self) -> None:
        with self.assertRaises(ReplayHistoryTooOld):
            build_fetch_depth_plan("M5", 1_700_000_000 + 15 * 86_400, 1_700_000_000)

    def test_dynamic_fetch_metadata_is_added_without_breaking_schema(self) -> None:
        provider = PaginatedProvider("binance")
        plan = build_fetch_depth_plan("M5", 1_700_200_000, 1_700_000_000)
        candles, diagnostic = fetch_with_depth_plan(provider, "BTC", "M5", plan)
        raw = {tf: provider.fetch_ohlcv("BTC", tf, 200, None) for tf in TIMEFRAMES}
        raw["M5"] = candles
        generation = build_symbol_generation("BTC", provider, raw, 1_700_200_000, fetch_diagnostics={"M5": diagnostic})
        frame = to_market_data_frame(generation["M5"])
        self.assertEqual(frame.status, "DATA_READY")
        self.assertEqual(generation["M5"]["requested_candle_count"], plan.requested_bars)

    def test_dynamic_fetch_result_passes_replay_coverage_gate(self) -> None:
        from market_reviewer.pipeline import replay_coverage_report

        previous = 1_700_000_000
        fetch_timestamp = previous + 2 * 86_400
        provider = PaginatedProvider("binance")
        plans = {"BTC": {tf: build_fetch_depth_plan(tf, fetch_timestamp, previous) for tf in TIMEFRAMES}}
        raw = {}
        diagnostics = {}
        for tf, plan in plans["BTC"].items():
            raw[tf], diagnostics[tf] = fetch_with_depth_plan(provider, "BTC", tf, plan)
        generation = {"BTC": build_symbol_generation("BTC", provider, raw, fetch_timestamp, fetch_diagnostics=diagnostics)}
        frames = {"BTC": {tf: to_market_data_frame(frame) for tf, frame in generation["BTC"].items()}}
        report = replay_coverage_report(frames, {"BTC": {"previous_review_timestamp": previous}})
        self.assertEqual(report["REPLAY_COVERAGE_COMPLETE"], "YES")


    def test_bootstrap_fetch_mode_allows_missing_state(self) -> None:
        plans = fetch_depth_plan_for_mode(1_700_200_000, FETCH_MODE_BOOTSTRAP, None)
        self.assertEqual(plans["BTC"]["M5"].requested_bars, DEFAULT_BOOTSTRAP_CANDLES)
        self.assertEqual(plans["ETH"]["M5"].replay_required_bars, 0)

    def test_production_replay_requires_state_path(self) -> None:
        with self.assertRaises(ReplayStateUnavailableForFetch):
            fetch_depth_plan_for_mode(1_700_200_000, FETCH_MODE_PRODUCTION_REPLAY, None)

    def test_production_replay_fails_closed_for_missing_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "review-state.v2.json"
            with self.assertRaisesRegex(ReplayStateUnavailableForFetch, "missing state file"):
                fetch_depth_plan_for_mode(1_700_200_000, FETCH_MODE_PRODUCTION_REPLAY, missing)

    def test_production_replay_fails_closed_for_malformed_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "review-state.v2.json"
            state.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(ReplayStateUnavailableForFetch, "malformed JSON"):
                load_required_replay_state(state)

    def test_production_replay_requires_all_symbol_review_timestamps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "review-state.v2.json"
            write_replay_state(state, btc_timestamp=1_700_000_000, eth_timestamp=None)
            with self.assertRaisesRegex(ReplayStateUnavailableForFetch, "ETH"):
                fetch_depth_plan_for_mode(1_700_200_000, FETCH_MODE_PRODUCTION_REPLAY, state)

    def test_production_replay_uses_dynamic_depth_from_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "review-state.v2.json"
            write_replay_state(state, btc_timestamp=1_700_000_000, eth_timestamp=1_700_000_000)
            plans = fetch_depth_plan_for_mode(1_700_200_000, FETCH_MODE_PRODUCTION_REPLAY, state)
            self.assertGreater(plans["BTC"]["M5"].replay_required_bars, 0)
            self.assertGreaterEqual(plans["BTC"]["M5"].requested_bars, DEFAULT_BOOTSTRAP_CANDLES)

    def test_observation44_expected_m5_depth_exceeds_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "review-state.v2.json"
            write_replay_state(state, btc_timestamp=1_787_622_000, eth_timestamp=1_787_622_000)
            plans = fetch_depth_plan_for_mode(1_787_720_674, FETCH_MODE_PRODUCTION_REPLAY, state)
            self.assertEqual(plans["BTC"]["M5"].replay_required_bars, 327)
            self.assertEqual(plans["BTC"]["M5"].requested_bars, 387)
            self.assertEqual(plans["BTC"]["M5"].coverage_start_timestamp, 1_787_607_300)

    def test_production_replay_preserves_history_too_old_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "review-state.v2.json"
            write_replay_state(state, btc_timestamp=1_700_000_000, eth_timestamp=1_700_000_000)
            with self.assertRaises(ReplayHistoryTooOld):
                fetch_depth_plan_for_mode(1_700_000_000 + 15 * 86_400, FETCH_MODE_PRODUCTION_REPLAY, state)

    def test_production_untouched_if_dynamic_fetch_fails(self) -> None:
        before = {"BTC": "state"}
        with self.assertRaises(RuntimeError):
            fetch_external_generation([PaginatedProvider("binance", set(TIMEFRAMES))], 1_700_200_000)
        self.assertEqual(before, {"BTC": "state"})

    def test_research_tracker_origin_expands_m5_fetch_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "review-state.v2.json"
            research = Path(directory) / "missed-opportunities.json"
            write_replay_state(state, btc_timestamp=1_787_850_000, eth_timestamp=1_787_850_000)
            persist_tracker_store(research, backfill_46_49("fixed"))
            plans = fetch_depth_plan_for_mode(1_787_916_300, FETCH_MODE_PRODUCTION_REPLAY, state, research)
            self.assertEqual(plans["BTC"]["M5"].previous_review_timestamp, 1_787_827_800)
            self.assertEqual(plans["BTC"]["M5"].coverage_start_timestamp, 1_787_813_100)
            self.assertGreater(plans["BTC"]["M5"].requested_bars, 200)

    def test_oldest_pending_tracker_selected_per_symbol(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "review-state.v2.json"
            research = Path(directory) / "missed-opportunities.json"
            write_replay_state(state, btc_timestamp=1_787_850_000, eth_timestamp=1_787_850_000)
            store = backfill_46_49("fixed")
            next(record for record in store["records"] if record["symbol"] == "ETH")["origin_snapshot_timestamp"] = 1_787_840_000
            persist_tracker_store(research, store)
            plans = fetch_depth_plan_for_mode(1_787_916_300, FETCH_MODE_PRODUCTION_REPLAY, state, research)
            self.assertEqual(plans["BTC"]["M5"].previous_review_timestamp, 1_787_827_800)
            self.assertEqual(plans["ETH"]["M5"].previous_review_timestamp, 1_787_839_800)

    def test_complete_trackers_do_not_expand_fetch_coverage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "review-state.v2.json"
            research = Path(directory) / "missed-opportunities.json"
            write_replay_state(state, btc_timestamp=1_787_850_000, eth_timestamp=1_787_850_000)
            store = backfill_46_49("fixed")
            for record in store["records"]:
                for outcome in record["outcomes"].values():
                    outcome["horizon_status"] = "COMPLETE"
            persist_tracker_store(research, store)
            plans = fetch_depth_plan_for_mode(1_787_916_300, FETCH_MODE_PRODUCTION_REPLAY, state, research)
            self.assertEqual(plans["BTC"]["M5"].previous_review_timestamp, 1_787_850_000)

    def test_required_first_m5_alignment_for_tracker_origin(self) -> None:
        from market_reviewer.missed_opportunity import first_required_m5_after_origin

        self.assertEqual(first_required_m5_after_origin(1_787_827_800), 1_787_828_100)


if __name__ == "__main__":
    unittest.main()
