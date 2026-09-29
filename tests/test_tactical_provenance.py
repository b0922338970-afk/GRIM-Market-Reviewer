import copy
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from market_reviewer.cli import main
from market_reviewer.tactical_provenance import build_exposure, capture_noncanonical, bind_tactical_identity, valid_exposure, SCHEMA
from market_reviewer.tactical_provenance_status import tactical_provenance_status
import test_live_hybrid_smc as live
import test_research_side_diagnostics as fixtures


def chain():
    direction = "BEARISH"
    review = {"Symbol": "BTC", "Review_Timestamp": "11700", "Swing_Bias": "BULLISH", "Displacement": "NONE",
        "Last_MSS": {"H1": "BEARISH 100 @ 3600", "M15": "BEARISH 100 @ 7200"},
        "Contextual_MSS": "BEARISH @ 7200; related_sweep_id=M5:4800; related_displacement_id=BEARISH:6000",
        "Active_Setup_ID": "M15-BEARISH-SETUP_FVG-8100", "Eligible_Retest_Confirmed": "YES",
        "Eligible_Retest_Setup_ID": "M15-BEARISH-SETUP_FVG-8100", "Eligible_Retest_Timestamp": "9000",
        "Eligible_Retest_Evidence_ID": "M15:9000"}
    def event(id, tf, ts, kind, evidence):
        return dict(event_id=id, symbol="BTC", timeframe=tf, timestamp=ts, type=kind, direction=direction,
                    available_at=12000, evidence=evidence)
    ds = event("D", "M15", 6000, "DISPLACEMENT", dict(direction=direction, strength="VALID", timestamp=6000,
         structure_broken="NONE", fvg_created=True, body_ratio=2, range_ratio=2, close_near_extreme=True, follow_through=False))
    sweeps = [dict(event(name, "M5", ts, kind, dict(timeframe="M5", timestamp=ts, event_type=kind,
        level_price=100, level_type="Internal Buy-side Liquidity")), pool_id="POOL", side="BUYSIDE")
        for name, ts, kind in (("S", 4800, "SWEPT"), ("R", 5100, "RECLAIMED"))]
    zone = dict(event("Z", "M15", 8100, "FVG", dict(direction=direction, timeframe="M15", formed_at=8100,
        status="FRESH", setup_type="SETUP_FVG", related_displacement_id="BEARISH:6000")), setup_id=review["Active_Setup_ID"])
    exp = dict(schema=SCHEMA, status="AVAILABLE", symbol="BTC", checkpoint=12000, production_selected_displacement="NONE",
        raw_directional_displacement=[ds], structure_events=[event("H", "H1", 3600, "MSS", {}), event("M", "M15", 7200, "MSS", {})],
        liquidity_events=sweeps, zones=[zone])
    return review, exp


class TacticalProvenanceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        live.LiveHybridSMCTests.setUpClass()
        cls.frames = live.LiveHybridSMCTests.frames
        cls.review = live.LiveHybridSMCTests.review
        cls.cp = int(cls.review["Review_Timestamp"]) + 300
        cls.exp = build_exposure(cls.frames, cls.review, cls.cp)

    def test_real_detector_exposure_valid(self):
        self.assertTrue(valid_exposure(self.exp, self.review, self.cp))

    def test_producer_keeps_both_direction_outputs(self):
        from market_reviewer import reviewer as rv
        ts = self.cp - 86400
        ds = [rv.DisplacementEvent(d, "VALID", ts, "NONE", False, 2, 2, True, False)
              for d in ("BULLISH", "BEARISH")]
        with patch.object(rv, "find_displacements", return_value=ds):
            result = build_exposure(self.frames, dict(self.review, Swing_Bias="BULLISH", Displacement="NONE"), self.cp)
        self.assertEqual({d["direction"] for d in result["raw_directional_displacement"]}, {"BULLISH", "BEARISH"})

    def test_selected_separate_and_no_input_mutation(self):
        before = copy.deepcopy((self.frames, self.review))
        result = build_exposure(self.frames, self.review, self.cp)
        self.assertEqual(result["production_selected_displacement"], self.review["Displacement"])
        self.assertEqual(before, (self.frames, self.review))
        altered = dict(self.review, Swing_Bias="NONE", Displacement="NONE")
        other = build_exposure(self.frames, altered, self.cp)
        self.assertEqual([d["event_id"] for d in result["raw_directional_displacement"]],
                         [d["event_id"] for d in other["raw_directional_displacement"]])

    def test_tf_availability_and_determinism(self):
        self.assertEqual(self.exp, build_exposure(self.frames, self.review, self.cp))
        for key in ("raw_directional_displacement", "structure_events", "liquidity_events", "zones"):
            for e in self.exp[key]:
                self.assertTrue(e["timeframe"])
                self.assertLessEqual(e["candle_close_timestamp"], e["available_at"])
                self.assertEqual(e["available_at"], self.cp)

    def test_m15_full_history(self):
        from market_reviewer import reviewer as rv
        from market_reviewer.live_hybrid_smc import legal_prefix
        frame = legal_prefix(self.frames, "BTC", self.cp)["M15"]
        expected = rv.analyze_structure(frame).events
        actual = [e for e in self.exp["structure_events"] if e["timeframe"] == "M15"]
        self.assertEqual(len(actual), len(expected))
        from dataclasses import replace
        original = rv.analyze_structure
        def structures(f):
            st = original(f)
            if f.timeframe == "M15":
                events = [rv.StructureEvent(d, 100, self.cp - delta, "BOS", "RANGE", d, "fixture")
                          for d, delta in (("BULLISH", 3600), ("BEARISH", 1800))]
                return replace(st, events=events)
            return st
        with patch.object(rv, "analyze_structure", side_effect=structures):
            result = build_exposure(self.frames, self.review, self.cp)
        self.assertEqual(len([e for e in result["structure_events"] if e["timeframe"] == "M15"]), 2)

    def test_future_candles_excluded(self):
        from market_reviewer.model import Candle
        f = copy.deepcopy(self.frames)
        for frame in f.values():
            frame.candles.append(Candle(self.cp + 86400, 100, 1000, 1, 900, 10))
        self.assertEqual(self.exp, build_exposure(f, self.review, self.cp))

    def test_no_secrets_or_paths(self):
        r = dict(self.review, token="secret123", local_path="C:/secret/data", outcomes={"future": 999})
        serialized = json.dumps(build_exposure(self.frames, r, self.cp))
        self.assertNotIn("secret123", serialized)
        self.assertNotIn("C:/", serialized)
        self.assertNotIn("future", serialized)

    def test_missing_anchor_no_proximity_binding(self):
        r, e = chain()
        r["Contextual_MSS"] = "NONE"
        self.assertEqual(bind_tactical_identity(r, e, 12000)["status"], "TACTICAL_IDENTITY_INCOMPLETE")

    def test_liquidity_inventory_not_linked_without_pool(self):
        r, e = chain()
        e["liquidity_events"][0]["pool_id"] = None
        self.assertEqual(bind_tactical_identity(r, e, 12000)["status"], "TACTICAL_IDENTITY_INCOMPLETE")

    def test_exact_existing_chain_uses_raw_not_selected(self):
        r, e = chain()
        before = copy.deepcopy((r, e))
        b = bind_tactical_identity(r, e, 12000)
        self.assertEqual(b["status"], "BOUND", b)
        self.assertEqual(b, bind_tactical_identity(r, e, 12000))
        self.assertEqual(before, (r, e))
        self.assertFalse(b["origin_creation_allowed"])

    def test_timeframe_collision_stays_incomplete(self):
        r, e = chain()
        e["raw_directional_displacement"].append(dict(e["raw_directional_displacement"][0], event_id="D2", timeframe="M5"))
        self.assertEqual(bind_tactical_identity(r, e, 12000)["status"], "TACTICAL_IDENTITY_INCOMPLETE")

    def test_prior_matching_m15_does_not_relax_latest_requirement(self):
        r, e = chain()
        r["Last_BOS"] = {"M15": "BULLISH 100 @ 9000"}
        self.assertEqual(bind_tactical_identity(r, e, 12000)["status"], "TACTICAL_IDENTITY_INCOMPLETE")

    def test_retest_identity_required(self):
        r, e = chain()
        r["Eligible_Retest_Setup_ID"] = "DIFFERENT"
        self.assertEqual(bind_tactical_identity(r, e, 12000)["status"], "TACTICAL_IDENTITY_INCOMPLETE")

    def test_detector_reference_normalized_only_after_exact_resolution(self):
        r, e = chain()
        e["zones"][0]["evidence"]["related_displacement_id"] = "M15:6000"
        self.assertEqual(bind_tactical_identity(r, e, 12000)["status"], "BOUND")
        e["zones"][0]["evidence"]["related_displacement_id"] = "H1:6000"
        self.assertEqual(bind_tactical_identity(r, e, 12000)["status"], "TACTICAL_IDENTITY_INCOMPLETE")

    def test_future_exposure_rejected(self):
        r, e = chain()
        e["raw_directional_displacement"][0]["available_at"] = 12001
        self.assertFalse(valid_exposure(e, r, 12000))

    def test_capture_failure_safe(self):
        r = capture_noncanonical({}, {"BTC": self.review}, self.cp)
        self.assertEqual(r["tactical_provenance"]["BTC"]["status"], "UNAVAILABLE")

    def test_wrong_direction_not_bound(self):
        r, e = chain()
        e["raw_directional_displacement"][0]["direction"] = "BULLISH"
        self.assertEqual(bind_tactical_identity(r, e, 12000)["status"], "TACTICAL_IDENTITY_INCOMPLETE")

    def test_checkpoint_and_review_identity_rejected(self):
        r, e = chain()
        self.assertFalse(valid_exposure(e, r, 12300))
        self.assertFalse(valid_exposure(e, dict(r, Symbol="ETH"), 12000))
        self.assertFalse(valid_exposure(e, dict(r, Displacement="DIFFERENT"), 12000))

    def test_runner_noncanonical_only(self):
        from pathlib import Path
        from market_reviewer.observation_runner import execute_production_observation
        reviews = {"BTC": self.review}
        with patch("market_reviewer.observation_runner.review_snapshot", return_value=reviews), \
             patch("market_reviewer.observation_runner._load_frames", return_value={"BTC": self.frames}), \
             patch("market_reviewer.observation_runner.extract_opportunity_snapshot", return_value={"unchanged": True}), \
             patch("market_reviewer.observation_runner._external_by_symbol", return_value={}), \
             patch("market_reviewer.observation_runner._sha256_or_none", return_value="CANONICAL_HASH"):
            result = execute_production_observation(dict(market_path="ignored", external_path="ignored", canonical_checkpoint=self.cp), 300, Path("ignored"))
        self.assertEqual(result["reviews"], reviews)
        self.assertEqual(result["production_hash"], "CANONICAL_HASH")
        self.assertEqual(result["canonical_checkpoint"], self.cp)
        self.assertEqual(result["non_canonical_research_evidence"]["tactical_provenance"]["BTC"]["status"], "AVAILABLE")

    def test_readonly_old_archive_remains_unavailable(self):
        f = fixtures.SideDiagnosticsTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        def contents():
            return {p: p.read_bytes() for p in f.f.base.rglob("*") if p.is_file()}
        before = contents()
        with patch("market_reviewer.missed_opportunity_live.upsert_tracker", side_effect=AssertionError("origin")):
            result = tactical_provenance_status(f.journal, f.f.live)
            self.assertEqual(result, tactical_provenance_status(f.journal, f.f.live))
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(main(["tactical-provenance-status", "--journal", str(f.journal), "--live-store", str(f.f.live), "--json"]), 0)
        self.assertEqual(result, json.loads(out.getvalue()))
        self.assertEqual(set(result["raw_displacement_coverage"]), {"RAW_EVIDENCE_UNAVAILABLE"})
        self.assertEqual(before, contents())
