from __future__ import annotations

import copy
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os._walk_symlinks_as_files = False

from market_reviewer import historical_replay as replay
from market_reviewer.cli import main
from market_reviewer.historical_data import (
    digest, external_at_checkpoint, frames_at_checkpoint, isolated_directory, load_historical_input, timestamp,
)
from market_reviewer.missed_opportunity import build_origin_candidate, create_tracker, empty_store
from market_reviewer.model import Candle, DataUnavailable, MarketDataFrame, TIMEFRAME_SECONDS

T = 1800057600


def frames_fixture(future_bars=300):
    frames = {}
    for tf, seconds in TIMEFRAME_SECONDS.items():
        candles = []
        for index in range(-70, future_bars + 1 if tf == "M5" else 3):
            price = 100 + (index % 9) * 0.1
            candles.append(Candle(T + index * seconds, price, price + 1, price - 1, price + 0.1, 10))
        frames[tf] = MarketDataFrame(
            symbol="BTC", timeframe=tf, source="fixture", provider="fixture", market_type="spot", timezone="UTC",
            dataset_id=tf, generation_id="fixture", generated_at="fixture", source_environment="TEST",
            completeness_status="DATA_READY", fetch_timestamp=candles[-1].timestamp + seconds,
            latest_candle_timestamp=candles[-1].timestamp, latest_closed_candle_timestamp=candles[-1].timestamp,
            current_open_candle_timestamp=None, candles=candles, status="DATA_READY",
        )
    return frames


def raw_input(frames):
    return {"BTC": {tf: {**{k: v for k, v in vars(frame).items() if k != "candles"}, "OHLCV": [vars(c) for c in frame.candles]}
                    for tf, frame in frames.items()}}


def eligible_feature():
    return {
        "snapshot_timestamp": T - 300, "sequence_state": "INVALIDATED",
        "truth": {"swing_bias": "BULLISH", "active_sweep": True},
        "features": {"HTF_ALIGNMENT": {"value": {"aligned_count": 2}},
                     "TREND_MATURITY": {"value": {"trend_maturity": "MATURE_CONTINUATION"}}},
        "raw_metrics": {}, "risk_signatures": [],
    }


def terminal_review():
    return {"Symbol": "BTC", "Sequence_ID": "BTC-seq-0001", "Sequence_State": "INVALIDATED",
            "Swing_Bias": "BULLISH", "State": "NO_TRADE", "Sequence_Transitions": []}


class HistoricalReplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.frames = frames_fixture()
        self.source = self.root / "input.json"
        self.source.write_text(json.dumps(raw_input(self.frames)), encoding="utf-8")

    def window(self, **kwargs):
        return replay.run_window(symbol="BTC", start=T, end=T + 300, source=self.source,
                                 output_dir=self.root / "out", cache_dir=self.root / "cache", **kwargs)

    def test_closed_and_higher_timeframe_closure(self):
        for tf, seconds in TIMEFRAME_SECONDS.items():
            before = frames_at_checkpoint(self.frames, T + seconds - 1)[tf]
            at = frames_at_checkpoint(self.frames, T + seconds)[tf]
            self.assertEqual(before.candles[-1].timestamp, T - seconds)
            self.assertEqual(at.candles[-1].timestamp, T)

    def test_no_future_or_open_candle_object_accessible(self):
        views = frames_at_checkpoint(self.frames, T)
        for tf, view in views.items():
            self.assertIsNone(view.current_open_candle_timestamp)
            self.assertTrue(all(c.timestamp + TIMEFRAME_SECONDS[tf] <= T for c in view.candles))
            self.assertEqual(view.fetch_timestamp, T)

    def test_future_candle_changes_do_not_change_decision(self):
        changed = copy.deepcopy(self.frames)
        for tf, frame in changed.items():
            frame.candles = [replace(c, high=999999) if c.timestamp >= T else c for c in frame.candles]
        a = replay.replay_frames(self.frames, symbol="BTC", start=T, end=T)
        b = replay.replay_frames(changed, symbol="BTC", start=T, end=T)
        self.assertEqual(a["decision_sha256"], b["decision_sha256"])

    def test_deterministic_observation_id_and_replay(self):
        a = replay.replay_frames(self.frames, symbol="BTC", start=T, end=T + 300)
        b = replay.replay_frames(self.frames, symbol="BTC", start=T, end=T + 300)
        self.assertEqual(a, b)
        self.assertTrue(a["observations"][0]["historical_observation_id"].startswith("H-BTC-"))

    def test_checkpoints_monotonic_and_internal_m5_not_skipped(self):
        result = replay.replay_frames(self.frames, symbol="BTC", start=T, end=T + 3600)
        self.assertEqual([o["checkpoint"] for o in result["observations"]], [T, T + 3600])
        self.assertEqual(result["internal_m5_checkpoint_count"], 13)

    def test_missing_m5_gap_blocked(self):
        self.frames["M5"].candles = [c for c in self.frames["M5"].candles if c.timestamp != T]
        with self.assertRaises(DataUnavailable):
            replay.replay_frames(self.frames, symbol="BTC", start=T, end=T + 300)

    def test_invalid_order_and_step_rejected(self):
        for args in ({"start": T, "end": T - 300}, {"start": T + 1, "end": T + 300},
                     {"start": T, "end": T, "step_minutes": 7}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                replay.replay_frames(self.frames, symbol="BTC", **args)

    def test_missing_external_is_unavailable_not_fabricated(self):
        visible = frames_at_checkpoint(self.frames, T)
        evidence = external_at_checkpoint("BTC", visible, T)
        for metric in evidence["raw_metrics"].values():
            self.assertEqual(metric["availability"], "UNAVAILABLE")
            self.assertIsNone(metric["value"])
        domains = replay.opportunity_evidence_from_snapshot(eligible_feature(), evidence)
        for domain in ("POSITIONING", "CROWDING", "LIQUIDATION_CONTEXT"):
            self.assertEqual(domains[domain], "DATA_UNAVAILABLE")

    def test_external_future_availability_excluded(self):
        from market_reviewer.external_evidence import metric
        item = metric("oi", 12, "fixture", T - 100, T + 1, "CURRENT")
        item["symbol"] = "BTC"
        evidence = external_at_checkpoint("BTC", frames_at_checkpoint(self.frames, T), T, [item])
        self.assertEqual(evidence["raw_metrics"]["oi"]["availability"], "UNAVAILABLE")

    def episode_store(self, count=2):
        store = empty_store(generated_at="synthetic")
        for n in range(count):
            t = T + n * 300
            feature = eligible_feature()
            feature["snapshot_timestamp"] = t - 300
            replay.apply_episode_observation(store, "BTC", terminal_review(), frames_at_checkpoint(self.frames, t),
                                             feature, None, t, f"H-{t}")
        return store

    def test_same_trajectory_single_episode_and_historical_id(self):
        store = self.episode_store()
        self.assertEqual(len(store["records"]), 1)
        self.assertEqual(len(store["records"][0]["snapshots"]), 2)
        self.assertTrue(store["records"][0]["tracker_id"].startswith("HIST-BTC-LONG-MOT-"))
        self.assertEqual(store, self.episode_store())

    def test_context_break_uses_live_semantics(self):
        store = self.episode_store()
        feature = eligible_feature()
        feature["features"] = {}
        feature["snapshot_timestamp"] = T + 300
        replay.apply_episode_observation(store, "BTC", terminal_review(), frames_at_checkpoint(self.frames, T + 600),
                                         feature, None, T + 600, "H-break")
        self.assertEqual(store["records"][0]["episode_status"], "BROKEN")

    def test_conversion_is_isolated_equivalent(self):
        store = self.episode_store()
        review = terminal_review()
        review["Sequence_ID"] = "BTC-seq-0002"
        review["Sequence_Transitions"] = [{"previous_state": "NONE", "new_state": "SEEKING_LIQUIDITY", "timestamp": T + 300}]
        replay.apply_episode_observation(store, "BTC", review, frames_at_checkpoint(self.frames, T + 600),
                                         eligible_feature(), None, T + 600, "H-converted")
        self.assertTrue(store["records"][0]["converted_to_production"])
        self.assertEqual(store["records"][0]["episode_status"], "CLOSED")

    def frozen_episode(self):
        doc = {"schema": replay.SCHEMA, "sample_source": "HISTORICAL_REPLAY", "start": T, "window_id": "one",
               "observations": [], "episodes": self.episode_store(1)["records"], "decisions_frozen": True}
        doc["decision_sha256"] = replay.decision_hash(doc)
        return doc

    def test_outcomes_require_freeze(self):
        doc = self.frozen_episode()
        doc["decisions_frozen"] = False
        with self.assertRaisesRegex(ValueError, "frozen"):
            replay.evaluate_outcomes(doc, self.frames["M5"])

    def test_outcomes_cannot_rewrite_snapshot_and_all_four_horizons(self):
        doc = self.frozen_episode()
        before = copy.deepcopy(doc)
        result = replay.evaluate_outcomes(doc, self.frames["M5"])
        self.assertEqual(doc, before)
        self.assertEqual(result["episodes"][0]["snapshots"], doc["episodes"][0]["snapshots"])
        for horizon in ("1H", "4H", "12H", "24H"):
            with self.subTest(horizon=horizon):
                outcome = result["episodes"][0]["outcomes"][horizon]
                self.assertEqual(outcome["horizon_status"], "COMPLETE")
                self.assertGreater(outcome["outcome_data_start"], T - 300)
                self.assertLessEqual(outcome["outcome_data_end"], outcome["window_end_timestamp"])
                self.assertIsNotNone(outcome["MFE_pct"])

    def test_origin_extreme_is_excluded_from_outcome(self):
        doc = self.frozen_episode()
        frame = copy.deepcopy(self.frames["M5"])
        frame.candles = [replace(c, high=99999) if c.timestamp == T - 300 else c for c in frame.candles]
        result = replay.evaluate_outcomes(doc, frame)
        self.assertLess(result["episodes"][0]["outcomes"]["1H"]["MFE_price"], 1000)

    def test_frozen_snapshot_tamper_rejected(self):
        doc = self.frozen_episode()
        doc["episodes"][0]["snapshots"][0]["price"] += 1
        with self.assertRaises(ValueError):
            replay.evaluate_outcomes(doc, self.frames["M5"])

    def test_completed_outcomes_immutable_on_shorter_future_input(self):
        done = replay.evaluate_outcomes(self.frozen_episode(), self.frames["M5"])
        again = replay.evaluate_outcomes(done, frames_at_checkpoint(self.frames, T)["M5"])
        self.assertEqual(done["episodes"], again["episodes"])

    def test_pending_outcomes_not_fake_complete(self):
        pending = replay.evaluate_outcomes(self.frozen_episode(), frames_at_checkpoint(self.frames, T)["M5"])
        self.assertTrue(all(o["horizon_status"] == "PENDING" for o in pending["episodes"][0]["outcomes"].values()))

    def test_overlap_dedup_and_alias_provenance(self):
        a = self.frozen_episode()
        b = copy.deepcopy(a)
        b["window_id"] = "two"
        result = replay.summarize([a, b])
        self.assertEqual(result["total_historical_episodes"], 1)
        self.assertEqual(len(result["episodes"][0]["alias_provenance"]), 2)

    def test_overlap_cold_start_alias(self):
        a = self.frozen_episode()
        a["episodes"] = self.episode_store(2)["records"]
        b = copy.deepcopy(a)
        b["window_id"] = "two"
        b["start"] += 300
        b["episodes"][0]["tracker_id"] += "-later-origin"
        b["episodes"][0]["snapshots"] = b["episodes"][0]["snapshots"][1:]
        self.assertEqual(replay.summarize([a, b])["total_historical_episodes"], 1)

    def test_source_counts_separate(self):
        summary = replay.summarize([self.frozen_episode()], {"records": [{}, {}]})
        self.assertEqual(summary["maturity_by_source"], {"LIVE": 2, "HISTORICAL_REPLAY": 1, "TOTAL": 3})

    def test_regime_label_is_analysis_only(self):
        a = self.window(window_id="a", analysis_regime_label="FAKE_BREAKOUT")
        b = self.window(window_id="b", analysis_regime_label="STRONG_UPTREND")
        self.assertEqual(a["decision_sha256"], b["decision_sha256"])
        self.assertEqual(a["analysis_context"]["type"], "ANALYSIS_LABEL")

    def test_completed_window_resume_does_not_rerun(self):
        first = self.window()
        with patch.object(replay, "replay_frames", side_effect=AssertionError("rerun")):
            second = self.window()
        self.assertEqual(second["resume_status"], "SKIPPED_COMPLETED")
        self.assertEqual(second["decision_sha256"], first["decision_sha256"])

    def test_completed_window_identity_conflict_rejected(self):
        self.window(window_id="same")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            self.window(window_id="same", step_minutes=5)

    def test_cache_reuse_and_metadata(self):
        _, meta = load_historical_input("BTC", self.source, self.root / "cache")
        self.source.unlink()
        frames, again = load_historical_input("BTC", None, self.root / "cache")
        self.assertEqual(meta, again)
        self.assertEqual(frames["M5"].provider, "fixture")
        self.assertEqual(again["sample_source"], "HISTORICAL_REPLAY")

    def test_cache_checksum_corruption_blocked(self):
        _, meta = load_historical_input("BTC", self.source, self.root / "cache")
        path = self.root / "cache" / (meta["sha256"] + ".json")
        data = json.loads(path.read_text())
        data["market_data"]["BTC"]["M5"]["OHLCV"][0]["high"] = 9999
        path.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, "checksum"):
            load_historical_input("BTC", None, self.root / "cache")

    def test_dry_run_bounded_no_writes(self):
        result = replay.run_window(symbol="BTC", start=T, end=T + 86400 * 365, source=self.source,
                                   cache_dir=self.root / "cache", output_dir=self.root / "out",
                                   dry_run=True, max_observations=1)
        self.assertEqual(len(result["observations"]), 1)
        self.assertFalse((self.root / "cache").exists())
        self.assertFalse((self.root / "out").exists())

    def test_batch_resume_safe(self):
        manifest = self.root / "windows.json"
        manifest.write_text(json.dumps({"schema": replay.MANIFEST_SCHEMA, "sample_source": "HISTORICAL_REPLAY",
            "windows": [{"window_id": "fixture", "symbol": "BTC", "start": T, "end": T,
                         "source": "input.json", "status": "READY", "sampling_reason": "TEST", "analysis_regime_label": None}]}))
        kwargs = {"output_dir": self.root / "out", "cache_dir": self.root / "cache"}
        self.assertEqual(replay.run_batch(manifest, **kwargs)["windows"][0]["status"], "COMPLETE")
        with patch.object(replay, "replay_frames", side_effect=AssertionError("rerun")):
            self.assertEqual(replay.run_batch(manifest, **kwargs)["windows"][0]["status"], "SKIPPED_COMPLETED")

    def test_all_artifacts_explicit_source(self):
        result = self.window()
        self.assertEqual(result["sample_source"], "HISTORICAL_REPLAY")
        for obs in result["observations"]:
            self.assertEqual(obs["sample_source"], "HISTORICAL_REPLAY")
            self.assertEqual(obs["decision_feature_snapshot"]["sample_source"], "HISTORICAL_REPLAY")

    def test_live_paths_forbidden(self):
        for name in ("reviews", "research", "artifact", "artifact/liquidations"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                isolated_directory(Path(name), "output")

    def test_live_sentinel_hashes_preserved(self):
        names = ("reviews/thesis-baseline.json", "research/missed-opportunities.json",
                 "artifact/production-observation-head.json", "artifact/observation-runner.json",
                 "artifact/observation-commit-journal.json", "artifact/liquidations/BTC-coverage.json")
        for name in names:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"live_sentinel": true}')
        before = {n: (self.root / n).read_bytes() for n in names}
        self.window()
        self.assertEqual(before, {n: (self.root / n).read_bytes() for n in names})

    def test_cli_dry_mode(self):
        with redirect_stdout(io.StringIO()) as output:
            code = main(["historical-replay", "--symbol", "BTC", "--start", str(T), "--end", str(T),
                         "--input", str(self.source), "--output-dir", str(self.root / "out"),
                         "--cache-dir", str(self.root / "cache"), "--dry-run"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "DRY_RUN")

    def test_timestamp_requires_explicit_timezone(self):
        self.assertEqual(timestamp("2026-01-01T00:00:00Z"), 1767225600)
        with self.assertRaises(ValueError):
            timestamp("2026-01-01T00:00:00")

    def assert_horizon_numbers(self, horizon, seconds):
        frozen = self.frozen_episode()
        origin = frozen["episodes"][0]["origin_snapshot_timestamp"]
        price = frozen["episodes"][0]["origin_price"]
        candles = [c for c in self.frames["M5"].closed_candles() if origin < c.timestamp <= origin + seconds]
        outcome = replay.evaluate_outcomes(frozen, self.frames["M5"])["episodes"][0]["outcomes"][horizon]
        self.assertEqual(outcome["horizon_status"], "COMPLETE")
        self.assertAlmostEqual(outcome["MFE_pct"], (max(c.high for c in candles) / price - 1) * 100)
        self.assertAlmostEqual(outcome["MAE_pct"], (min(c.low for c in candles) / price - 1) * 100)

    def test_1h_outcome_numbers(self):
        self.assert_horizon_numbers("1H", 3600)

    def test_4h_outcome_numbers(self):
        self.assert_horizon_numbers("4H", 14400)

    def test_12h_outcome_numbers(self):
        self.assert_horizon_numbers("12H", 43200)

    def test_24h_outcome_numbers(self):
        self.assert_horizon_numbers("24H", 86400)

    def test_external_eligible_at_boundary_is_reused(self):
        from market_reviewer.external_evidence import metric
        item = metric("oi", 12, "fixture", T - 100, T, "CURRENT", symbol="BTC")
        evidence = external_at_checkpoint("BTC", frames_at_checkpoint(self.frames, T), T, [item])
        self.assertEqual(evidence["raw_metrics"]["oi"]["value"], 12)
        self.assertEqual(evidence["raw_metrics"]["oi"]["availability"], "AVAILABLE")

    def test_source_future_rejected_even_with_old_availability(self):
        from market_reviewer.external_evidence import metric
        item = metric("oi", 12, "fixture", T + 1, T, "CURRENT", symbol="BTC")
        evidence = external_at_checkpoint("BTC", frames_at_checkpoint(self.frames, T), T, [item])
        self.assertEqual(evidence["raw_metrics"]["oi"]["availability"], "UNAVAILABLE")

    def test_eth_real_engine_path(self):
        frames = copy.deepcopy(self.frames)
        for frame in frames.values():
            frame.symbol = "ETH"
        result = replay.replay_frames(frames, symbol="ETH", start=T, end=T)
        self.assertEqual(result["observations"][0]["review"]["Symbol"], "ETH")
        self.assertTrue(result["observations"][0]["historical_observation_id"].startswith("H-ETH-"))

    def test_live_entrypoints_never_called(self):
        with patch("market_reviewer.pipeline.review_snapshot", side_effect=AssertionError("live review")), \
             patch("market_reviewer.persistence.persist_review_state", side_effect=AssertionError("live persist")), \
             patch("market_reviewer.missed_opportunity_live.persist_tracker_store", side_effect=AssertionError("live research")), \
             patch("market_reviewer.observation_runner.run_observation_loop", side_effect=AssertionError("runner")):
            self.window()

    def test_initial_manifest_only_placeholders(self):
        path = Path(__file__).resolve().parents[1] / "historical/windows.initial.json"
        result = replay.run_batch(path, output_dir=self.root / "out", cache_dir=self.root / "cache")
        self.assertTrue(all(w["status"] == "NOT_SELECTED" for w in result["windows"]))
        self.assertFalse((self.root / "out").exists())

    def test_window_traversal_rejected(self):
        with self.assertRaises(ValueError):
            self.window(window_id="../live")

    def test_short_outcome_uses_existing_formula(self):
        frozen = self.frozen_episode()
        frozen["episodes"][0]["direction"] = "SHORT"
        frozen["decision_sha256"] = replay.decision_hash(frozen)
        result = replay.evaluate_outcomes(frozen, self.frames["M5"])
        self.assertEqual(result["episodes"][0]["outcomes"]["1H"]["horizon_status"], "COMPLETE")
        self.assertLess(result["episodes"][0]["outcomes"]["1H"]["MFE_price"],
                        result["episodes"][0]["outcomes"]["1H"]["MAE_price"])

    def test_ambiguous_overlapping_grid_not_counted_twice(self):
        a = self.frozen_episode()
        a["episodes"] = self.episode_store(3)["records"]
        b = copy.deepcopy(a)
        b["window_id"] = "shifted"
        b["start"] += 150
        episode = b["episodes"][0]
        episode["tracker_id"] += "-shifted"
        episode["origin_snapshot_timestamp"] += 150
        for snapshot in episode["snapshots"]:
            snapshot["snapshot_timestamp"] += 150
        summary = replay.summarize([a, b])
        self.assertEqual(summary["raw_window_episode_count"], 2)
        self.assertEqual(summary["total_historical_episodes"], 1)
        self.assertEqual(summary["unresolved_overlap_count"], 1)

    def test_source_symbol_mismatch_blocked(self):
        raw = raw_input(self.frames)
        for frame in raw["BTC"].values():
            frame["symbol"] = "ETH"
        self.source.write_text(json.dumps(raw))
        with self.assertRaisesRegex(DataUnavailable, "symbol mismatch"):
            load_historical_input("BTC", self.source, self.root / "cache")

    def test_source_false_closed_marker_blocked(self):
        raw = raw_input(self.frames)
        raw["BTC"]["M5"]["fetch_timestamp"] -= 1
        self.source.write_text(json.dumps(raw))
        with self.assertRaisesRegex(DataUnavailable, "not-yet-closed"):
            load_historical_input("BTC", self.source, self.root / "cache")
