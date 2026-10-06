import copy
import json
import subprocess
import sys
import unittest

from market_reviewer import tactical_shadow_capture as capture
from market_reviewer.tactical_sequence_metrics import sequence_metrics
import test_tactical_sequence_acceptance_audit as fixtures


def record(obs, state, transitions):
    return {"observation_number": obs, "states": {"BTC": {
        "sequence_id": "BTC-1", "sequence_state": state}}, "transitions": transitions}


def transition(state, reason="START"):
    return {"symbol": "BTC", "sequence_id": "BTC-1", "to_state": state, "reason": reason}


class SequenceMetricTests(unittest.TestCase):
    def cohort(self):
        return [record(301, "FORMING", [transition("FORMING")]),
                record(302, "INVALIDATED", [transition("INVALIDATED", "TACTICAL_DIRECTION_WITHDRAWN")])]

    def test_unique_and_transition_counts_separate(self):
        rows = self.cohort()
        rows.append(record(303, "INVALIDATED", [transition("INVALIDATED", "TACTICAL_DIRECTION_REVERSAL")]))
        result = sequence_metrics(rows)
        self.assertEqual(result["invalidated_unique_sequences"], 1)
        self.assertEqual(result["invalidation_transitions"], 2)
        self.assertEqual(result["withdrawn_transitions"], 1)
        self.assertEqual(result["reversal_transitions"], 1)
        self.assertEqual(result["conservation"]["status"], "FAIL")

    def test_repeated_receipt_not_new_sequence_or_transition(self):
        rows = self.cohort()
        rows.append(record(303, "INVALIDATED", []))
        result = sequence_metrics(rows)
        self.assertEqual(result["unique_sequence_ids"], 1)
        self.assertEqual(result["invalidation_transitions"], 1)
        self.assertEqual(result["conservation"]["status"], "PASS")

    def test_current_head_not_ever_invalidated_count(self):
        rows = self.cohort()
        new = record(303, "FORMING", [transition("FORMING")])
        new["states"]["BTC"]["sequence_id"] = "BTC-2"
        new["transitions"][0]["sequence_id"] = "BTC-2"
        result = sequence_metrics(rows + [new])
        self.assertEqual(result["invalidated_unique_sequences"], 1)
        self.assertEqual(result["current_invalidated_sequences"], 0)
        self.assertEqual(result["still_active_sequences"], 1)
        self.assertEqual(result["conservation"]["status"], "PASS")

    def test_other_reason_preserved(self):
        rows = self.cohort()
        rows[1]["transitions"][0]["reason"] = "LOCKED_SETUP_INVALIDATED"
        self.assertEqual(sequence_metrics(rows)["other_invalidation_transitions"], 1)

    def test_determinism_and_no_mutation(self):
        rows = self.cohort()
        before = copy.deepcopy(rows)
        self.assertEqual(sequence_metrics(rows), sequence_metrics(rows))
        self.assertEqual(rows, before)

    def test_fresh_process_determinism(self):
        rows = self.cohort()
        code = "import json,sys; from market_reviewer.tactical_sequence_metrics import sequence_metrics; print(json.dumps(sequence_metrics(json.load(sys.stdin))))"
        output = subprocess.check_output([sys.executable, "-c", code], input=json.dumps(rows), text=True)
        self.assertEqual(json.loads(output), sequence_metrics(rows))

    def test_status_audit_same_source_read_only_and_gates_unchanged(self):
        fixture = fixtures.AcceptanceAuditTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.cohort(forming=True)
        before = {p: p.read_bytes() for p in fixture.root.rglob("*") if p.is_file()}
        live = capture.live_status(fixture.root)
        audit = fixture.report()
        self.assertEqual(live["status"], "PASS")
        self.assertEqual(live["sequence_metrics"], audit["sequence_metrics"])
        self.assertEqual(live["invalidated"], live["invalidation_transitions"])
        self.assertEqual(audit["invalidations"]["total"], live["invalidated_unique_sequences"])
        self.assertTrue(live["SHADOW_SEQUENCE_CAPTURE_READY"])
        self.assertFalse(live["SHADOW_SEQUENCE_VALIDATED"])
        self.assertEqual(before, {p: p.read_bytes() for p in fixture.root.rglob("*") if p.is_file()})

    def test_scope_fingerprint_identifies_different_prefix(self):
        rows = self.cohort()
        self.assertNotEqual(sequence_metrics(rows)["source"], sequence_metrics(rows[:1])["source"])

    def test_absent_ledger_metrics_unavailable(self):
        import tempfile
        with tempfile.TemporaryDirectory() as root:
            self.assertIsNone(capture.live_status(root)["sequence_metrics"])
