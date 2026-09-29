import copy
import hashlib
import io
import json
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from market_reviewer.cli import main
from market_reviewer.tactical_evidence_audit import audit_review, tactical_evidence_audit, CAPABILITIES
import test_research_side_diagnostics as fixtures
import test_tactical_direction_shadow as shadow_fixtures


class TacticalEvidenceAuditTests(unittest.TestCase):
    def trace(self, review=None):
        return audit_review(review if review is not None else shadow_fixtures.review(), 12000, 247, "BTC")

    def test_unknown_is_not_absence(self):
        r = self.trace({})
        self.assertEqual(r["requirements"]["directional_displacement"]["binding_status"], "UNKNOWN")
        self.assertEqual(r["RAW_BEARISH_DISPLACEMENT_AVAILABLE"], "UNKNOWN")
        self.assertEqual(r["countertrend_readiness_bucket"], "E_UNKNOWN")

    def test_selected_none_does_not_prove_raw_absent(self):
        r = shadow_fixtures.review()
        r["Displacement"] = "NONE"
        t = self.trace(r)
        self.assertEqual(t["requirements"]["directional_displacement"]["missing_reason"], "SELECTED_NONE_RAW_UNKNOWN")
        self.assertEqual(CAPABILITIES["displacement"]["raw_list_exposure"], "COMPUTED_NOT_EXPOSED")

    def test_direction_mismatch_is_not_bound(self):
        r = shadow_fixtures.review()
        r["Displacement"] = "BULLISH VALID @ 6000; structure_broken=YES"
        t = self.trace(r)["requirements"]["directional_displacement"]
        self.assertEqual(t["missing_reason"], "SELECTED_OPPOSITE_DIRECTION")
        self.assertFalse(t["existing_shadow_requirement_pass"])

    def test_timeframe_must_be_explicit(self):
        r = shadow_fixtures.review()
        r["Displacement"] = "BEARISH VALID @ 6000; structure_broken=YES"
        t = self.trace(r)["requirements"]["directional_displacement"]
        self.assertTrue(t["existing_shadow_requirement_pass"])
        self.assertEqual(t["binding_status"], "AVAILABLE_NOT_BOUND")
        self.assertEqual(t["source_timeframe"], "NOT_EXPOSED")

    def test_liquidity_presence_not_linkage(self):
        r = shadow_fixtures.review()
        r["Liquidity_Events"] = [{"timeframe": "M5", "timestamp": 4800, "event_type": "SWEPT"}]
        t = self.trace(r)["requirements"]["liquidity"]
        self.assertTrue(t["value"])
        self.assertEqual(t["binding_status"], "AVAILABLE_NOT_BOUND")
        self.assertFalse(t["existing_shadow_requirement_pass"])

    def test_setup_identity_not_guessed(self):
        r = self.trace()["requirements"]["setup"]
        self.assertEqual(r["missing_reason"], "TACTICAL_CHAIN_IDENTITY_MISSING")
        self.assertIsNone(r["value"]["active_setup_id"])

    def test_m15_opposite_not_missing_market(self):
        r = shadow_fixtures.review()
        r["Last_MSS"]["M15"] = "BULLISH 100 @ 7200"
        t = self.trace(r)["requirements"]["M15_confirmation"]
        self.assertEqual(t["missing_reason"], "M15_OPPOSITE_DIRECTION")
        self.assertEqual(t["binding_status"], "AVAILABLE_NOT_BOUND")

    def test_future_inventory_not_used(self):
        r = shadow_fixtures.review()
        r["FVG"][0]["formed_at"] = 12000
        self.assertEqual(self.trace(r)["requirements"]["setup"]["value"]["FVG"], [])

    def test_existing_shadow_order_is_reported_not_changed(self):
        r = shadow_fixtures.review()
        r["Displacement"] = "BEARISH VALID @ 3300; structure_broken=YES"
        self.assertEqual(self.trace(r)["requirements"]["directional_displacement"]["missing_reason"],
                         "SELECTED_PRECEDES_H1_STRUCTURE_SHADOW_ORDER")

    def test_input_immutable_and_outcomes_ignored(self):
        r = shadow_fixtures.review()
        original = copy.deepcopy(r)
        first = self.trace(r)
        self.assertEqual(original, r)
        r["outcomes"] = {"24H": {"MFE": 9999}}
        self.assertEqual(first, self.trace(r))
        self.assertFalse(first["origin_creation_allowed"])

    def test_readonly_deterministic_cli_and_gate_isolation(self):
        f = fixtures.SideDiagnosticsTests()
        f.setUp()
        self.addCleanup(f.doCleanups)
        def hashes():
            return {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in f.f.base.rglob("*") if p.is_file()}
        before = hashes()
        with patch("market_reviewer.missed_opportunity_live.is_eligible_origin", side_effect=AssertionError("eligibility")), \
             patch("market_reviewer.missed_opportunity_live.upsert_tracker", side_effect=AssertionError("origin")):
            result = tactical_evidence_audit(f.journal, f.f.live)
            self.assertEqual(result, tactical_evidence_audit(f.journal, f.f.live))
            output = io.StringIO()
            with redirect_stdout(output):
                rc = main(["tactical-evidence-audit", "--journal", str(f.journal), "--live-store", str(f.f.live), "--json"])
        self.assertEqual(rc, 0)
        self.assertEqual(result, json.loads(output.getvalue()))
        self.assertEqual(before, hashes())
        self.assertEqual(result["status"], "PASS")

    def test_missing_archive_fail_closed(self):
        result = tactical_evidence_audit("nonexistent-audit-journal", "nonexistent-audit-store")
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertNotIn("traces", result)

    def test_detector_selection_contract(self):
        from market_reviewer.reviewer import DisplacementEvent, _latest_displacement
        bullish = DisplacementEvent("BULLISH", "VALID", 100, "NONE", False, 2, 2, True, False)
        bearish = DisplacementEvent("BEARISH", "VALID", 200, "NONE", False, 2, 2, True, False)
        raw = [bullish, bearish]
        self.assertIs(_latest_displacement(raw, "BULLISH"), bullish)
        self.assertIs(_latest_displacement(raw, "BEARISH"), bearish)
        self.assertIsNone(_latest_displacement(raw, "NONE"))
        self.assertEqual(len(raw), 2)
