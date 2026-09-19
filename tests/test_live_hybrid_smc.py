import copy
import hashlib
import inspect
import json
import math
import unittest
from pathlib import Path

from market_reviewer.live_hybrid_smc import build_live_hybrid_smc, legal_prefix
from market_reviewer.live_smc_frozen_context import capture_live_smc_context
from market_reviewer.smc_live_sample_status import _frozen_live
from market_reviewer.external import build_symbol_generation
from market_reviewer.model import Candle, TIMEFRAME_SECONDS, to_market_data_frame
from market_reviewer.reviewer import review_symbol
from market_reviewer.opportunity import extract_opportunity_snapshot
from tests.test_pipeline import FakeProvider
from tests.test_hybrid_attribution import inputs
from historical_research.hybrid_attribution import resolve


class LiveHybridSMCTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cp = 1728000000
        raw = {}
        for tf, seconds in TIMEFRAME_SECONDS.items():
            raw[tf] = [Candle(cp - (60-i)*seconds, 100+i/10+math.sin(i)*3,
                             105+i/10+math.sin(i)*3, 97+i/10+math.sin(i)*3,
                             102+i/10+math.sin(i)*3, 10) for i in range(60)]
        cls.frames = {tf: to_market_data_frame(f) for tf, f in build_symbol_generation("BTC", FakeProvider(), raw, cp).items()}
        cls.review = review_symbol(cls.frames).to_dict()
        cls.opp = extract_opportunity_snapshot(cls.review, cls.frames)
        cls.record = {"tracker_id": "test-origin", "origin_snapshot_timestamp": cp-300,
                      "origin_observation": 1, "symbol": "BTC", "direction": "LONG"}
        cls.result = build_live_hybrid_smc(cls.record, cls.review, cls.opp, cls.frames)

    def test_real_engine_produces_payload(self):
        self.assertEqual(self.result["status"], "AVAILABLE", self.result["reason"])
        self.assertEqual(set(self.result["smc_state"]["matching_context"]),
            {"symbol","direction","phase","regime","HTF","location","extension","liquidity_type","displacement"})

    def test_deterministic_and_production_unchanged(self):
        before = copy.deepcopy((self.record, self.review, self.opp, self.frames))
        self.assertEqual(build_live_hybrid_smc(self.record, self.review, self.opp, self.frames), self.result)
        self.assertEqual(before, (self.record, self.review, self.opp, self.frames))

    def test_future_candles_cannot_change_payload(self):
        frames = copy.deepcopy(self.frames)
        cp = self.record["origin_snapshot_timestamp"] + 300
        for tf, f in frames.items():
            f.candles.append(Candle(cp+TIMEFRAME_SECONDS[tf], 999, 9999, 1, 888, 10))
            f.latest_candle_timestamp = f.candles[-1].timestamp
        self.assertEqual(build_live_hybrid_smc(self.record, self.review, self.opp, frames), self.result)

    def test_all_timeframe_boundaries(self):
        cp = self.record["origin_snapshot_timestamp"] + 300
        for tf, f in legal_prefix(self.frames, "BTC", cp).items():
            self.assertTrue(all(c.timestamp+TIMEFRAME_SECONDS[tf] <= cp for c in f.candles))
            self.assertIsNone(f.current_open_candle_timestamp)

    def test_missing_frame_unavailable_not_fabricated(self):
        f = dict(self.frames)
        del f["H1"]
        result = build_live_hybrid_smc(self.record, self.review, self.opp, f)
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertIsNone(result["smc_state"])

    def test_outcomes_not_consumed(self):
        record = dict(self.record, outcomes={"24H": {"MFE_pct": 999999}})
        opp = dict(self.opp, outcomes={"anything": "future"})
        self.assertEqual(build_live_hybrid_smc(record, self.review, opp, self.frames), self.result)

    def test_capture_and_status_without_outcomes(self):
        self.assertEqual(self.result["status"], "AVAILABLE", self.result["reason"])
        opp = dict(self.opp, smc_state=self.result["smc_state"])
        frozen = capture_live_smc_context(self.record, self.review, opp, self.record["origin_snapshot_timestamp"])
        self.assertEqual(frozen["classification_status"], "CLASSIFIABLE", frozen)
        r = dict(self.record, snapshots=[{"snapshot_timestamp": self.record["origin_snapshot_timestamp"],
             "observation_number": 1, "available_at": frozen["available_at"], "live_smc_frozen_context": frozen}])
        self.assertEqual(_frozen_live(r), self.result["smc_state"])

    def test_single_parent_uses_existing_authority(self):
        self.assertEqual(resolve(*inputs())["parents"]["D"]["authority_type"], "UNAMBIGUOUS_SINGLE_EVENT_PARENT")

    def test_cluster_parent_uses_existing_authority(self):
        self.assertEqual(resolve(*inputs(True))["parents"]["D"]["authority_type"], "COHERENT_CLUSTER_PARENT")

    def test_extractor_body_matches_frozen_historical_source(self):
        # Parity check is read-only and skipped where runtime archival files are absent.
        p = Path("research/historical-replay/smc-quality-v1/analyze.py")
        if not p.exists():
            self.skipTest("optional historical archive unavailable")
        from historical_research import smc_origin_context
        old = p.read_text(encoding="utf-8")
        body = old[old.index("def family("):old.index("\ndef main(")].strip()
        current = inspect.getsource(smc_origin_context)
        self.assertEqual(current[current.index("def family("):].strip(), body)


if __name__ == "__main__":
    unittest.main()
