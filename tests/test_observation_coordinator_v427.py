from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer.external_evidence import metric
from market_reviewer.liquidation_collector import CoverageStore
from market_reviewer.missed_opportunity import backfill_46_49, persist_tracker_store
from market_reviewer.observation_coordinator import READY, WAITING_FOR_M5_CLOSE, prepare_observation
from market_reviewer.persistence import atomic_write_json


SYMBOLS = ("BTC", "ETH")
TIMEFRAMES = ("D1", "H4", "H1", "M15", "M5")
SECONDS = {"D1": 86400, "H4": 14400, "H1": 3600, "M15": 900, "M5": 300}


def write_state(path: Path, timestamp: int = 1_787_934_000) -> None:
    atomic_write_json(
        path,
        {
            "persistence_version": 2,
            "state_schema": "review-state.v2",
            "symbols": {symbol: {"previous_review_timestamp": timestamp} for symbol in SYMBOLS},
        },
    )


def candles_until(latest_open: int, count: int = 60, timeframe: str = "M5") -> list[dict]:
    seconds = SECONDS[timeframe]
    first = latest_open - (count - 1) * seconds
    return [
        {"timestamp": first + index * seconds, "open": 100 + index, "high": 101 + index, "low": 99 + index, "close": 100.5 + index, "volume": 10}
        for index in range(count)
    ]


def market_snapshot(m5_latest_open: int, current_open: int | None = None) -> dict:
    snapshot = {}
    for symbol in SYMBOLS:
        frames = {}
        for timeframe in TIMEFRAMES:
            latest = m5_latest_open if timeframe == "M5" else m5_latest_open - (m5_latest_open % SECONDS[timeframe]) - SECONDS[timeframe]
            candles = candles_until(latest, timeframe=timeframe)
            if timeframe == "M5" and current_open is not None:
                candles.append({"timestamp": current_open, "open": 200, "high": 201, "low": 199, "close": 200.5, "volume": 10})
            frames[timeframe] = {
                "symbol": symbol,
                "timeframe": timeframe,
                "source": "fixture",
                "provider": "binance",
                "market_type": "crypto_spot",
                "timezone": "UTC",
                "dataset_id": f"fixture:{symbol}:{timeframe}",
                "generation_id": "fixture",
                "generated_at": "fixture",
                "source_environment": "test",
                "completeness_status": "DATA_READY",
                "fetch_timestamp": latest + SECONDS[timeframe],
                "latest_candle_timestamp": candles[-1]["timestamp"],
                "latest_closed_candle_timestamp": latest,
                "current_open_candle_timestamp": current_open if timeframe == "M5" else None,
                "OHLCV": candles,
                "status": "DATA_READY",
                "warnings": [],
                "requested_candle_count": 200,
                "returned_candle_count": len(candles),
                "replay_required_bars": 0,
                "coverage_start_timestamp": None,
                "pagination_pages": 1,
            }
        snapshot[symbol] = frames
    return snapshot


def external_artifact(required_time: int) -> dict:
    payload = {"artifact_version": "fixture", "schema_version": "external-market-evidence.v1", "provider": "fixture", "fetch_timestamp": required_time, "symbols": {}}
    for symbol in SYMBOLS:
        metrics = {
            "oi": metric("oi", 100.0, "fixture", required_time, required_time, "current"),
            "oi_change_5m": metric("oi_change_5m", 1.0, "fixture", required_time, required_time, "5m"),
            "oi_change_15m": metric("oi_change_15m", 1.0, "fixture", required_time, required_time, "15m"),
            "oi_change_1h": metric("oi_change_1h", 1.0, "fixture", required_time, required_time, "1h"),
            "oi_change_4h": metric("oi_change_4h", 1.0, "fixture", required_time, required_time, "4h"),
            "funding_rate": metric("funding_rate", 0.0001, "fixture", required_time, required_time, "current"),
            "funding_change": metric("funding_change", 0.0, "fixture", required_time, required_time, "settled"),
            "funding_percentile": metric("funding_percentile", 50.0, "fixture", required_time, required_time, "history"),
        }
        payload["symbols"][symbol] = {"evidence": {"raw_metrics": metrics, "snapshot_timestamp": required_time}, "diagnostics": {}}
    return payload


def complete_liquidation(root: Path, start: int, end: int) -> None:
    for symbol in SYMBOLS:
        CoverageStore(root).save(
            symbol,
            {
                "store_version": "liquidation-event-store.v1",
                "symbol": symbol,
                "collector_started_at": start,
                "collector_stopped_at": end,
                "last_transport_alive_at": end,
                "last_stream_message_at": start,
                "last_event_received_at": None,
                "disconnect_intervals": [],
                "coverage_intervals": [{"start": start, "end": end, "status": "CONNECTED"}],
                "coverage_status": "COMPLETE",
            },
        )


class Fetcher:
    def __init__(self, artifacts: list[dict], filename: str) -> None:
        self.artifacts = artifacts
        self.filename = filename
        self.calls = 0
        self.written: list[dict] = []

    def __call__(self, output_dir: Path) -> Path:
        artifact = self.artifacts[min(self.calls, len(self.artifacts) - 1)]
        self.calls += 1
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / self.filename
        path.write_text(json.dumps(artifact, indent=2, sort_keys=True), encoding="utf-8")
        self.written.append(copy.deepcopy(artifact))
        return path


class ObservationCoordinatorV427Tests(unittest.TestCase):
    def run_prepare(self, market_artifacts: list[dict], external_time: int, liquidity_end: int | None = None, complete_outcomes: bool = True, state_timestamp: int = 1_787_934_000) -> tuple[dict, Fetcher, Fetcher, Path, Path]:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        state = root / "reviews" / "thesis-baseline.json"
        research = root / "research" / "missed-opportunities.json"
        write_state(state, state_timestamp)
        store = backfill_46_49("fixed")
        if complete_outcomes:
            for record in store["records"]:
                for outcome in record["outcomes"].values():
                    outcome["horizon_status"] = "COMPLETE"
        persist_tracker_store(research, store)
        liq_end = liquidity_end if liquidity_end is not None else external_time + 3600
        complete_liquidation(root / "artifact" / "liquidations", external_time - 7200, liq_end)
        market = Fetcher(market_artifacts, "market-data-v1.json")
        external = Fetcher([external_artifact(external_time)], "external-market-evidence-v1.json")
        result = prepare_observation(
            output_dir=root / "artifact",
            state_path=state,
            research_tracker_path=research,
            liquidation_root=root / "artifact" / "liquidations",
            market_fetcher=market,
            external_fetcher=external,
        )
        return result, market, external, state, research

    def test_external_later_than_checkpoint_selects_next_m5(self) -> None:
        result, market, _, _, _ = self.run_prepare([market_snapshot(1_787_934_600), market_snapshot(1_787_934_900)], 1_787_935_044)
        self.assertEqual(result["status"], READY)
        self.assertEqual(result["canonical_checkpoint"], 1_787_935_200)
        self.assertEqual(result["market_refresh_count"], 1)
        self.assertEqual(market.calls, 2)

    def test_exact_equality_is_eligible(self) -> None:
        result, _, _, _, _ = self.run_prepare([market_snapshot(1_787_934_900)], 1_787_935_200)
        self.assertEqual(result["status"], READY)
        self.assertEqual(result["canonical_checkpoint"], 1_787_935_200)
        self.assertEqual(result["external_ready"], "YES")

    def test_external_earlier_than_checkpoint_no_extra_market_refresh(self) -> None:
        result, market, _, _, _ = self.run_prepare([market_snapshot(1_787_935_200)], 1_787_935_044)
        self.assertEqual(result["status"], READY)
        self.assertEqual(result["market_refresh_count"], 0)
        self.assertEqual(market.calls, 1)

    def test_external_inside_open_candle_returns_waiting(self) -> None:
        result, _, _, _, _ = self.run_prepare([market_snapshot(1_787_934_600, current_open=1_787_934_900), market_snapshot(1_787_934_600, current_open=1_787_934_900)], 1_787_935_044)
        self.assertEqual(result["status"], WAITING_FOR_M5_CLOSE)
        self.assertEqual(result["next_required_m5_open"], 1_787_934_900)
        self.assertIsNone(result["canonical_checkpoint"])

    def test_frozen_external_snapshot_reused_after_market_refresh(self) -> None:
        result, _, external, _, _ = self.run_prepare([market_snapshot(1_787_934_600), market_snapshot(1_787_934_900)], 1_787_935_044)
        self.assertEqual(result["status"], READY)
        self.assertEqual(external.calls, 1)
        self.assertEqual(result["external_snapshot_timestamp"], 1_787_935_044)

    def test_no_second_external_fetch(self) -> None:
        _, _, external, _, _ = self.run_prepare([market_snapshot(1_787_934_600), market_snapshot(1_787_934_900)], 1_787_935_044)
        self.assertEqual(external.calls, 1)

    def test_max_two_market_fetches(self) -> None:
        result, market, _, _, _ = self.run_prepare([market_snapshot(1_787_934_000), market_snapshot(1_787_934_300), market_snapshot(1_787_934_900)], 1_787_935_044)
        self.assertEqual(result["status"], WAITING_FOR_M5_CLOSE)
        self.assertEqual(market.calls, 2)

    def test_no_open_candle_checkpoint(self) -> None:
        result, _, _, _, _ = self.run_prepare([market_snapshot(1_787_934_600, current_open=1_787_934_900), market_snapshot(1_787_934_600, current_open=1_787_934_900)], 1_787_935_200)
        self.assertEqual(result["status"], WAITING_FOR_M5_CLOSE)

    def test_no_timestamp_rewriting(self) -> None:
        result, _, _, _, _ = self.run_prepare([market_snapshot(1_787_934_900)], 1_787_935_044)
        item = result["external_freeze"]["required_fields"]["BTC"]["funding_rate"]
        self.assertEqual(item["source_timestamp"], 1_787_935_044)
        self.assertEqual(item["available_at"], 1_787_935_044)

    def test_review51_race_fixture_resolves(self) -> None:
        result, _, external, _, _ = self.run_prepare([market_snapshot(1_787_934_600), market_snapshot(1_787_934_900)], 1_787_935_044)
        self.assertEqual(result["canonical_checkpoint"], 1_787_935_200)
        self.assertEqual(result["market_refresh_count"], 1)
        self.assertEqual(result["external_fetch_count"], 1)
        self.assertEqual(external.calls, 1)

    def test_review52_race_fixture_resolves(self) -> None:
        result, _, _, _, _ = self.run_prepare([market_snapshot(1_787_940_300), market_snapshot(1_787_940_600)], 1_787_940_845)
        self.assertEqual(result["canonical_checkpoint"], 1_787_940_900)
        self.assertEqual(result["status"], READY)

    def test_already_aligned_fixture_ready(self) -> None:
        result, _, _, _, _ = self.run_prepare([market_snapshot(1_787_941_200)], 1_787_940_845)
        self.assertEqual(result["status"], READY)
        self.assertEqual(result["market_refresh_count"], 0)


    def test_general_checkpoint_never_moves_backward(self) -> None:
        result, _, _, _, _ = self.run_prepare([market_snapshot(1_787_941_200)], 1_787_940_845)
        self.assertEqual(result["status"], READY)
        self.assertGreaterEqual(result["canonical_checkpoint"], result["initial_checkpoint"])
        self.assertGreaterEqual(result["next_required_checkpoint"], result["initial_checkpoint"])

    def test_previous_production_lower_bound_respected(self) -> None:
        previous_open = 1_787_934_900
        result, _, _, _, _ = self.run_prepare(
            [market_snapshot(1_787_935_200)],
            1_787_934_845,
            state_timestamp=previous_open,
        )
        self.assertEqual(result["status"], READY)
        self.assertEqual(result["canonical_checkpoint"], 1_787_935_500)
        self.assertGreater(result["canonical_checkpoint"] - 300, previous_open)

    def test_waiting_target_never_moves_backward(self) -> None:
        result, _, _, _, _ = self.run_prepare(
            [market_snapshot(1_787_934_600), market_snapshot(1_787_934_600)],
            1_787_935_044,
        )
        self.assertEqual(result["status"], WAITING_FOR_M5_CLOSE)
        self.assertGreaterEqual(result["next_required_checkpoint"], result["initial_checkpoint"])

    def test_liquidation_coverage_checked_after_checkpoint_freeze(self) -> None:
        result, _, _, _, _ = self.run_prepare([market_snapshot(1_787_934_900)], 1_787_935_044, liquidity_end=1_787_935_200)
        self.assertEqual(result["status"], READY)
        self.assertEqual(result["liquidation"]["symbols"]["BTC"]["5m"]["coverage_status"], "COMPLETE")

    def test_liquidation_gap_blocks_readiness(self) -> None:
        result, _, _, _, _ = self.run_prepare([market_snapshot(1_787_934_900)], 1_787_935_044, liquidity_end=1_787_934_950)
        self.assertEqual(result["status"], "BLOCKED")
        self.assertEqual(result["reason"], "LIQUIDATION_NOT_READY")

    def test_research_outcome_deeper_fetch_compatibility(self) -> None:
        result, _, _, _, _ = self.run_prepare([market_snapshot(1_787_934_900)], 1_787_935_044, complete_outcomes=False)
        self.assertIn("BTC", result["research_required_starts"])

    def test_completed_horizons_do_not_expand_fetch(self) -> None:
        result, _, _, _, _ = self.run_prepare([market_snapshot(1_787_934_900)], 1_787_935_044, complete_outcomes=True)
        self.assertEqual(result["research_required_starts"], {})

    def test_completed_outcomes_remain_immutable(self) -> None:
        result, _, _, _, research = self.run_prepare([market_snapshot(1_787_934_900)], 1_787_935_044)
        self.assertTrue(result["state_immutability"]["research_unchanged"])
        self.assertEqual(hashlib.sha256(research.read_bytes()).hexdigest(), result["state_immutability"]["research_before_sha256"])

    def test_production_state_unchanged(self) -> None:
        result, _, _, state, _ = self.run_prepare([market_snapshot(1_787_934_900)], 1_787_935_044)
        self.assertTrue(result["state_immutability"]["production_unchanged"])
        self.assertEqual(hashlib.sha256(state.read_bytes()).hexdigest(), result["state_immutability"]["production_before_sha256"])

    def test_research_state_unchanged(self) -> None:
        result, _, _, _, _ = self.run_prepare([market_snapshot(1_787_934_900)], 1_787_935_044)
        self.assertEqual(result["state_immutability"]["research_unchanged"], True)

    def test_restart_deterministic(self) -> None:
        first, _, _, _, _ = self.run_prepare([market_snapshot(1_787_934_600), market_snapshot(1_787_934_900)], 1_787_935_044)
        second, _, _, _, _ = self.run_prepare([market_snapshot(1_787_934_600), market_snapshot(1_787_934_900)], 1_787_935_044)
        keys = ("status", "canonical_checkpoint", "required_external_time", "market_refresh_count", "external_fetch_count")
        self.assertEqual({key: first[key] for key in keys}, {key: second[key] for key in keys})


if __name__ == "__main__":
    unittest.main()