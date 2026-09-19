import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from market_reviewer.live_smc_frozen_context import capture_live_smc_context
from market_reviewer.missed_opportunity import empty_store, deterministic_tracker_id, persist_tracker_store, load_tracker_store
from market_reviewer.missed_opportunity_live import build_tracker_candidate_from_observation, dry_run_missed_opportunity_observation, apply_missed_opportunity_observation
from market_reviewer.smc_live_sample_status import _frozen_live
from test_missed_opportunity_live_v426a import SNAPSHOT_TS, terminal_review, opportunity, external, frame


class LiveSMCFrozenContextTests(unittest.TestCase):
    def setUp(self):
        producer = patch("market_reviewer.live_hybrid_smc.build_live_hybrid_smc",
                         side_effect=lambda record, review, opportunity, frames: {"smc_state": opportunity.get("smc_state")})
        producer.start()
        self.addCleanup(producer.stop)
        self.review = terminal_review()
        self.review.update(Current_Phase="PULLBACK", Market_Regime="TREND_PULLBACK",
                           Structure_State={"D1": "BULLISH", "H4": "BULLISH"})
        m5 = frame("BTC", start=SNAPSHOT_TS, count=1)
        self.frames = {tf: m5 for tf in ("M5", "M15", "H1", "H4", "D1")}
        self.opp = opportunity()
        self.opp["truth"]["active_sweep"] = True
        candidate = build_tracker_candidate_from_observation(symbol="BTC", review=self.review,
            frames=self.frames, opportunity_snapshot=self.opp, external_evidence=external(), observation_number=50)
        self.eid = deterministic_tracker_id("BTC", "LONG", SNAPSHOT_TS, candidate["context_signature"])

    def complete_source(self):
        context = dict(symbol="BTC", direction="LONG", phase="PULLBACK", regime="TREND_PULLBACK",
                       HTF=["BULLISH", "BULLISH"], location="DISCOUNT", extension="NOT_FLAGGED",
                       liquidity_type="EXTERNAL", displacement="VALID")
        self.opp["smc_state"] = dict(schema="smc-hybrid-outcome.v1", episode_id=self.eid,
            symbol="BTC", direction="LONG", phase="PULLBACK", checkpoint=SNAPSHOT_TS + 300,
            matching_context=context, fields=dict(MSS="CONTEXTUAL", BOS="GENERIC",
                FVG_vs_BPR="CHAIN_FVG_NO_BPR", parent_authority="UNAMBIGUOUS_SINGLE_EVENT_PARENT",
                OB_vs_BREAKER="OTHER_OB_CONTEXT", reaction="SWEEP_RECLAIMED", liquidity_context="REVERSAL_CHAIN",
                FVG="CHAIN_LINKED_FRESH", BPR="NONE", OB="CHAIN_LINKED_FRESH", BREAKER="NOT_CONFIRMED"))

    def apply(self, store=None, observation=50):
        return dry_run_missed_opportunity_observation(store=empty_store() if store is None else store,
            reviews={"BTC": self.review}, frames={"BTC": self.frames}, opportunity_snapshots={"BTC": self.opp},
            external_evidence={"BTC": external()}, observation_number=observation)[0]

    def payload(self, store):
        return store["records"][0]["snapshots"][0]["live_smc_frozen_context"]

    def test_origin_creation_freezes_unknown_without_affecting_eligibility(self):
        store = self.apply()
        p = self.payload(store)
        self.assertEqual(len(store["records"]), 1)
        self.assertEqual(p["schema"], "live-smc-frozen-context.v1")
        self.assertEqual(p["phase"], "PULLBACK")
        self.assertEqual(p["hybrid_parent_authority"], "UNAVAILABLE")
        self.assertIn("hybrid_parent_authority", p["missing_fields"])

    def test_complete_origin_is_classifiable_before_outcome(self):
        self.complete_source()
        store = self.apply()
        self.assertEqual(self.payload(store)["classification_status"], "CLASSIFIABLE")
        self.assertEqual(_frozen_live(store["records"][0]), self.opp["smc_state"])
        self.assertTrue(all(v["horizon_status"] != "COMPLETE" for v in store["records"][0]["outcomes"].values()))

    def test_payload_immutable_on_followup(self):
        self.complete_source()
        store = self.apply()
        before = copy.deepcopy(self.payload(store))
        self.opp["snapshot_timestamp"] += 300
        self.opp["smc_state"]["fields"]["MSS"] = "GENERIC"
        with patch("market_reviewer.live_smc_frozen_context.capture_live_smc_context", side_effect=AssertionError("not a new origin")):
            after = self.apply(store, 51)
        self.assertEqual(self.payload(after), before)
        self.assertNotIn("live_smc_frozen_context", after["records"][0]["snapshots"][-1])

    def test_missing_never_backfilled(self):
        store = self.apply()
        before = copy.deepcopy(self.payload(store))
        self.complete_source()
        after = self.apply(store, 51)
        self.assertEqual(self.payload(after), before)
        self.assertEqual(before["classification_status"], "LIVE_NOT_CLASSIFIABLE")

    def test_legacy_origin_never_backfilled(self):
        store = self.apply()
        del store["records"][0]["snapshots"][0]["live_smc_frozen_context"]
        self.complete_source()
        after = self.apply(store, 51)
        self.assertNotIn("live_smc_frozen_context", after["records"][0]["snapshots"][0])
        with self.assertRaisesRegex(ValueError, "MISSING_FROZEN"):
            _frozen_live(after["records"][0])

    def test_future_smc_evidence_rejected(self):
        self.complete_source()
        self.opp["smc_state"]["events"] = [{"available_at": SNAPSHOT_TS + 301}]
        p = self.payload(self.apply())
        self.assertEqual(p["classification_status"], "LIVE_NOT_CLASSIFIABLE")
        self.assertIsNone(p["smc_state"])

    def test_boundary_mismatch_never_uses_later_context(self):
        self.complete_source()
        self.opp["snapshot_timestamp"] -= 300
        p = self.payload(self.apply())
        self.assertEqual(p["capture_reason"], "ORIGIN_MARKET_BOUNDARY_MISMATCH")
        self.assertEqual(p["phase"], "UNAVAILABLE")

    def test_input_production_and_outcome_access_isolation(self):
        self.complete_source()
        before = json.dumps([self.review, self.opp], sort_keys=True)
        store = self.apply()
        self.assertEqual(json.dumps([self.review, self.opp], sort_keys=True), before)
        r = {k: v for k, v in store["records"][0].items() if k != "outcomes"}
        self.assertEqual(capture_live_smc_context(r, self.review, self.opp, SNAPSHOT_TS),
                         {k: v for k, v in self.payload(store).items() if k != "producer"})

    def test_deterministic_payload(self):
        self.complete_source()
        self.assertEqual(self.payload(self.apply()), self.payload(self.apply()))

    def test_capture_is_deep_copy(self):
        self.complete_source()
        store = self.apply()
        self.opp["smc_state"]["fields"]["MSS"] = "GENERIC"
        self.assertEqual(self.payload(store)["contextual_mss"], "CONTEXTUAL")
        self.assertEqual(self.payload(store)["smc_state"]["fields"]["MSS"], "CONTEXTUAL")

    def test_persist_reload_and_retry_reuses_frozen_payload(self):
        self.complete_source()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "research.json"
            result = apply_missed_opportunity_observation(reviews={"BTC": self.review}, frames={"BTC": self.frames},
                opportunity_snapshots={"BTC": self.opp}, external_evidence={"BTC": external()},
                observation_number=50, store_path=path)
            self.assertEqual(result["research_persistence"], "PASS")
            store = load_tracker_store(path)
            frozen = self.payload(store)
            self.assertEqual(self.payload(self.apply(store)), frozen)
            self.assertEqual(_frozen_live(store["records"][0]), self.opp["smc_state"])

    def test_source_absent_does_not_import_or_run_historical_reconstruction(self):
        with patch("builtins.open", side_effect=AssertionError("no file access")):
            store = self.apply()
        self.assertEqual(self.payload(store)["capture_reason"], "FROZEN_HYBRID_SOURCE_UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
