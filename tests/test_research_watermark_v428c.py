from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path

os._walk_symlinks_as_files = False

from market_reviewer.external_evidence import build_external_market_evidence, metric
from market_reviewer.missed_opportunity import (
    latest_processed_observation,
    latest_tracker_snapshot_observation,
    load_tracker_store,
    mark_research_observation_processed,
)
from market_reviewer.missed_opportunity_live import apply_missed_opportunity_observation, missed_opportunity_status
from market_reviewer.model import Candle, MarketDataFrame
from market_reviewer.observation_coordinator import READY
from market_reviewer.observation_runner import (
    BLOCKED_RECOVERY_METADATA_MISSING,
    RunnerConfig,
    next_observation_number,
    observation_runner_status,
    run_observation_loop,
)
from market_reviewer.persistence import atomic_write_json


def write_json(path: Path, data: dict) -> None:
    atomic_write_json(path, data)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def candle(timestamp: int, close: float) -> Candle:
    return Candle(timestamp=timestamp, open=close, high=close + 1, low=close - 1, close=close, volume=1)


def frame(symbol: str = "BTC", price: float = 100.0) -> MarketDataFrame:
    candles = [candle(1_000 + index * 300, price + index) for index in range(80)]
    latest = candles[-1].timestamp
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
        latest_candle_timestamp=latest,
        latest_closed_candle_timestamp=latest,
        current_open_candle_timestamp=None,
        candles=candles,
        status="DATA_READY",
    )


def frames(symbol: str = "BTC", price: float = 100.0) -> dict[str, MarketDataFrame]:
    m5 = frame(symbol, price)
    return {tf: m5 for tf in ("D1", "H4", "H1", "M15", "M5")}


def review(symbol: str = "BTC", state: str = "INVALIDATED") -> dict:
    return {
        "Symbol": symbol,
        "Sequence_ID": f"{symbol}-seq-0009",
        "Sequence_State": state,
        "State": "NO_TRADE",
        "Swing_Bias": "BULLISH",
        "Review_Timestamp": "24700",
        "Sequence_Transitions": [],
    }


def opportunity(symbol: str = "BTC", momentum: str = "MATURE_CONTINUATION") -> dict:
    return {
        "symbol": symbol,
        "sequence_id": f"{symbol}-seq-0009",
        "sequence_state": "INVALIDATED",
        "snapshot_timestamp": 24_700,
        "opportunity_status": "NO_ACTIVE_OPPORTUNITY",
        "truth": {"swing_bias": "BULLISH", "active_sweep": False},
        "raw_metrics": {"LIQUIDITY": {"liquidity_distance_pct": None}},
        "features": {
            "HTF_ALIGNMENT": {"value": {"aligned_count": 2}},
            "DISPLACEMENT_STRENGTH": {"value": {"status": "MISSING"}},
            "TREND_MATURITY": {"value": {"trend_maturity": momentum}},
            "POI_FRESHNESS": {"value": {"status": "DATA_UNAVAILABLE"}},
            "REGIME": {"value": {"regime": "TREND_CONTINUATION"}},
        },
        "risk_signatures": [],
    }


def external(symbol: str = "BTC") -> dict:
    ts = 24_700
    return build_external_market_evidence(symbol, ts, {
        "oi_change_1h": metric("oi_change_1h", 10, "fixture", ts, ts, "1h"),
        "funding_rate": metric("funding_rate", 0.0001, "fixture", ts, ts, "current"),
    }, {"price_change_pct": 1.0})


def research_store(latest: int = 101, broken: bool = True) -> dict:
    return {
        "schema": "missed-opportunity-tracker.v1",
        "generated_at": "fixed",
        "records": [
            {
                "tracker_id": "BTC-LONG-MOT-1",
                "symbol": "BTC",
                "direction": "LONG",
                "status": "DETERIORATING",
                "episode_status": "BROKEN" if broken else "OPEN",
                "origin_observation": 46,
                "origin_snapshot_timestamp": 1_000,
                "origin_price": 100.0,
                "converted_to_production": False,
                "snapshots": [{"observation_number": latest, "snapshot_timestamp": latest * 100, "price": 100.0, "trajectory_state": "STABLE"}],
                "outcomes": {"1H": {"horizon_status": "COMPLETE", "MFE_pct": 1}, "4H": {"horizon_status": "COMPLETE"}, "12H": {"horizon_status": "COMPLETE"}, "24H": {"horizon_status": "COMPLETE"}},
            }
        ],
    }


def production_state(timestamp: int = 10200) -> dict:
    return {"persistence_version": 2, "state_schema": "review-state.v2", "symbols": {"BTC": {"previous_review_timestamp": timestamp}, "ETH": {"previous_review_timestamp": timestamp}}}


def write_head(path: Path, observation: int, state_hash: str, checkpoint: int = 10_200) -> None:
    write_json(path, {"schema": "production-observation-head.v1", "observation_number": observation, "canonical_checkpoint": checkpoint, "production_previous_review_timestamp": {"BTC": checkpoint, "ETH": checkpoint}, "production_state_sha256": state_hash, "cycle_id": f"cycle-{observation}", "committed_at": checkpoint})


def complete_tx(observation: int, production_hash: str, checkpoint: int = 10_200) -> dict:
    return {
        "cycle_id": f"cycle-{observation}",
        "observation_number": observation,
        "canonical_checkpoint": checkpoint,
        "market_path": "artifact/market-data-v1.json",
        "external_path": "artifact/external-market-evidence-v1.json",
        "status": "COMPLETE",
        "research_status": "COMPLETE",
        "production_hash": production_hash,
        "research_state_sha256": "before",
        "recovery_payload": {
            "observation_number": observation,
            "market_path": "artifact/market-data-v1.json",
            "external_path": "artifact/external-market-evidence-v1.json",
            "production_hash": production_hash,
            "reviews": {"BTC": review("BTC")},
            "opportunity_snapshots": {"BTC": opportunity("BTC")},
            "external_evidence": {"BTC": external("BTC")},
        },
    }


def ready(root: Path) -> dict:
    return {"status": READY, "canonical_checkpoint": 10_200, "market_path": str(root / "artifact" / "market-data-v1.json"), "external_path": str(root / "artifact" / "external-market-evidence-v1.json")}


class Clock:
    def __init__(self, value: int) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value


class ResearchWatermarkV428cTests(unittest.TestCase):
    def test_research_cycle_with_tracker_append_advances_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            report = apply_missed_opportunity_observation(reviews={"BTC": review("BTC")}, frames={"BTC": frames("BTC")}, opportunity_snapshots={"BTC": opportunity("BTC")}, external_evidence={"BTC": external("BTC")}, observation_number=102, store_path=path, canonical_checkpoint=10_200, production_state_sha256="prod", persist=True)
            self.assertEqual(report["research_result"], "TRACKER_UPDATED")
            self.assertEqual(report["latest_processed_observation"], 102)
            self.assertEqual(report["latest_tracker_snapshot_observation"], 102)

    def test_zero_append_still_advances_watermark(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            write_json(path, research_store(latest=101, broken=True))
            before_records = copy.deepcopy(load_tracker_store(path)["records"])
            report = apply_missed_opportunity_observation(reviews={"BTC": review("BTC")}, frames={"BTC": frames("BTC")}, opportunity_snapshots={"BTC": opportunity("BTC", momentum="UNALIGNED")}, external_evidence={"BTC": external("BTC")}, observation_number=102, store_path=path, canonical_checkpoint=10_200, production_state_sha256="prod", persist=True)
            store = load_tracker_store(path)
            self.assertEqual(report["research_result"], "NO_TRACKER_APPEND")
            self.assertEqual(latest_processed_observation(store), 102)
            self.assertEqual(latest_tracker_snapshot_observation(store), 101)
            self.assertEqual(store["records"], before_records)

    def test_latest_research_observation_uses_watermark(self) -> None:
        store = research_store(latest=101)
        mark_research_observation_processed(store, observation_number=102, canonical_checkpoint=10_200, production_state_sha256="prod", processed_at=10_201, research_result="NO_TRACKER_APPEND")
        self.assertEqual(latest_processed_observation(store), 102)

    def test_latest_tracker_snapshot_observation_remains_separate(self) -> None:
        store = research_store(latest=101)
        mark_research_observation_processed(store, observation_number=102, canonical_checkpoint=10_200, production_state_sha256="prod", processed_at=10_201, research_result="NO_TRACKER_APPEND")
        self.assertEqual(latest_tracker_snapshot_observation(store), 101)


    def test_status_exposes_watermark_and_tracker_latest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            store = research_store(latest=101)
            mark_research_observation_processed(store, observation_number=102, canonical_checkpoint=10_200, production_state_sha256="prod", processed_at=1, research_result="NO_TRACKER_APPEND")
            write_json(path, store)
            status = missed_opportunity_status(path)
            self.assertEqual(status["latest_processed_observation"], 102)
            self.assertEqual(status["latest_tracker_snapshot_observation"], 101)
    def test_102_style_zero_append_does_not_create_tracker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "research" / "missed-opportunities.json"
            write_json(path, {"schema": "missed-opportunity-tracker.v1", "records": []})
            report = apply_missed_opportunity_observation(reviews={"BTC": review("BTC")}, frames={"BTC": frames("BTC")}, opportunity_snapshots={"BTC": opportunity("BTC", momentum="UNALIGNED")}, external_evidence={"BTC": external("BTC")}, observation_number=102, store_path=path, canonical_checkpoint=10_200, production_state_sha256="prod", persist=True)
            store = load_tracker_store(path)
            self.assertEqual(report["research_result"], "NO_TRACKER_APPEND")
            self.assertEqual(store["records"], [])

    def test_broken_tracker_unchanged(self) -> None:
        store = research_store(latest=101, broken=True)
        before = copy.deepcopy(store["records"][0])
        mark_research_observation_processed(store, observation_number=102, canonical_checkpoint=10_200, production_state_sha256="prod", processed_at=10_201, research_result="NO_TRACKER_APPEND")
        self.assertEqual(store["records"][0], before)

    def test_complete_outcomes_immutable(self) -> None:
        store = research_store(latest=101, broken=True)
        before = copy.deepcopy(store["records"][0]["outcomes"])
        mark_research_observation_processed(store, observation_number=102, canonical_checkpoint=10_200, production_state_sha256="prod", processed_at=10_201, research_result="NO_TRACKER_APPEND")
        self.assertEqual(store["records"][0]["outcomes"], before)

    def test_duplicate_watermark_write_idempotent(self) -> None:
        store = research_store(latest=101)
        first = mark_research_observation_processed(store, observation_number=102, canonical_checkpoint=10_200, production_state_sha256="prod", processed_at=1, research_result="NO_TRACKER_APPEND")
        second = mark_research_observation_processed(first, observation_number=102, canonical_checkpoint=10_200, production_state_sha256="prod", processed_at=1, research_result="NO_TRACKER_APPEND")
        self.assertEqual(first, second)

    def test_wrong_production_hash_rejected(self) -> None:
        store = research_store(latest=101)
        mark_research_observation_processed(store, observation_number=102, canonical_checkpoint=10_200, production_state_sha256="prod", processed_at=1, research_result="NO_TRACKER_APPEND")
        with self.assertRaises(ValueError):
            mark_research_observation_processed(store, observation_number=102, canonical_checkpoint=10_200, production_state_sha256="other", processed_at=1, research_result="NO_TRACKER_APPEND")

    def test_wrong_checkpoint_rejected(self) -> None:
        store = research_store(latest=101)
        mark_research_observation_processed(store, observation_number=102, canonical_checkpoint=10_200, production_state_sha256="prod", processed_at=1, research_result="NO_TRACKER_APPEND")
        with self.assertRaises(ValueError):
            mark_research_observation_processed(store, observation_number=102, canonical_checkpoint=10_500, production_state_sha256="prod", processed_at=1, research_result="NO_TRACKER_APPEND")

    def test_old_store_without_watermark_bootstraps_from_tracker_history(self) -> None:
        store = research_store(latest=101)
        self.assertEqual(latest_processed_observation(store), 101)
        self.assertNotIn("research_observation_watermark", store)

    def test_runner_production_102_watermark_102_is_normal(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "reviews" / "thesis-baseline.json", production_state())
            write_json(root / "research" / "missed-opportunities.json", research_store(latest=101))
            cfg = RunnerConfig(output_dir=root / "artifact", state_path=root / "reviews" / "thesis-baseline.json", research_tracker_path=root / "research" / "missed-opportunities.json", max_cycles=0)
            prod_hash = sha(cfg.state_path)
            write_head(cfg.production_head_path, 102, prod_hash)
            store = load_tracker_store(cfg.research_tracker_path)
            mark_research_observation_processed(store, observation_number=102, canonical_checkpoint=10_200, production_state_sha256=prod_hash, processed_at=10_201, research_result="NO_TRACKER_APPEND")
            write_json(cfg.research_tracker_path, store)
            status = observation_runner_status(cfg.runner_state_path, state_path=cfg.state_path, research_tracker_path=cfg.research_tracker_path, production_head_path=cfg.production_head_path, clock=Clock(1))
            self.assertEqual(status["recovery_status"], "NORMAL")

    def test_runner_production_102_watermark_101_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "reviews" / "thesis-baseline.json", production_state())
            write_json(root / "research" / "missed-opportunities.json", research_store(latest=101))
            cfg = RunnerConfig(output_dir=root / "artifact", state_path=root / "reviews" / "thesis-baseline.json", research_tracker_path=root / "research" / "missed-opportunities.json", max_cycles=0)
            write_head(cfg.production_head_path, 102, sha(cfg.state_path))
            status = observation_runner_status(cfg.runner_state_path, state_path=cfg.state_path, research_tracker_path=cfg.research_tracker_path, production_head_path=cfg.production_head_path, clock=Clock(1))
            self.assertEqual(status["recovery_status"], BLOCKED_RECOVERY_METADATA_MISSING)

    def test_tracker_latest_101_watermark_102_valid(self) -> None:
        store = research_store(latest=101)
        mark_research_observation_processed(store, observation_number=102, canonical_checkpoint=10_200, production_state_sha256="prod", processed_at=1, research_result="NO_TRACKER_APPEND")
        self.assertEqual(latest_tracker_snapshot_observation(store), 101)
        self.assertEqual(latest_processed_observation(store), 102)

    def test_103_numbering_allowed_after_watermark_102(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "reviews" / "thesis-baseline.json", production_state())
            store = research_store(latest=101)
            mark_research_observation_processed(store, observation_number=102, canonical_checkpoint=10_200, production_state_sha256="prod", processed_at=1, research_result="NO_TRACKER_APPEND")
            write_json(root / "research" / "missed-opportunities.json", store)
            write_json(root / "artifact" / "observation-commit-journal.json", {"schema": "observation-commit-journal.v1", "transactions": []})
            write_head(root / "artifact" / "production-observation-head.json", 102, sha(root / "reviews" / "thesis-baseline.json"))
            self.assertEqual(next_observation_number(root / "research" / "missed-opportunities.json", root / "artifact" / "observation-commit-journal.json", root / "artifact" / "production-observation-head.json"), 103)

    def test_zero_append_cycle_restart_safe(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "reviews" / "thesis-baseline.json", production_state())
            write_json(root / "research" / "missed-opportunities.json", research_store(latest=101))
            cfg = RunnerConfig(output_dir=root / "artifact", state_path=root / "reviews" / "thesis-baseline.json", research_tracker_path=root / "research" / "missed-opportunities.json", max_cycles=0)
            prod_hash = sha(cfg.state_path)
            write_head(cfg.production_head_path, 102, prod_hash)
            write_json(cfg.commit_journal_path, {"schema": "observation-commit-journal.v1", "transactions": [complete_tx(102, prod_hash)]})
            result = run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=lambda p, n: {}, research_executor=lambda p: {"research_persistence": "FAIL"}, clock=Clock(10_300))
            self.assertEqual(result["recovered_pending_research"]["recovery"], "COMPLETED_RESEARCH_WATERMARK_REPAIRED")
            status = observation_runner_status(cfg.runner_state_path, state_path=cfg.state_path, research_tracker_path=cfg.research_tracker_path, journal_path=cfg.commit_journal_path, production_head_path=cfg.production_head_path, clock=Clock(10_300))
            self.assertEqual(status["research_latest_observation"], 102)

    def test_journal_complete_missing_watermark_repaired_safely(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_json(root / "reviews" / "thesis-baseline.json", production_state())
            write_json(root / "research" / "missed-opportunities.json", research_store(latest=101))
            cfg = RunnerConfig(output_dir=root / "artifact", state_path=root / "reviews" / "thesis-baseline.json", research_tracker_path=root / "research" / "missed-opportunities.json", max_cycles=0)
            prod_hash = sha(cfg.state_path)
            write_head(cfg.production_head_path, 102, prod_hash)
            write_json(cfg.commit_journal_path, {"schema": "observation-commit-journal.v1", "transactions": [complete_tx(102, prod_hash)]})
            before_records = copy.deepcopy(load_tracker_store(cfg.research_tracker_path)["records"])
            run_observation_loop(cfg, coordinator=lambda **_: ready(root), production_executor=lambda p, n: {}, research_executor=lambda p: {"research_persistence": "FAIL"}, clock=Clock(10_300))
            store = load_tracker_store(cfg.research_tracker_path)
            self.assertEqual(latest_processed_observation(store), 102)
            self.assertEqual(latest_tracker_snapshot_observation(store), 101)
            self.assertEqual(store["records"], before_records)


if __name__ == "__main__":
    unittest.main()