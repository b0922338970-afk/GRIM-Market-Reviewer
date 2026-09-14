import copy
import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

from market_reviewer.cli import main
from market_reviewer.smc_live_sample_status import ARMS, HORIZONS, NA, smc_live_sample_status
from test_smc_sample_status import FIXTURE, sha


class SMCLiveSampleStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "historical"
        self.root.mkdir()
        fixture = json.loads(FIXTURE.read_text())
        contexts = fixture["contexts"]
        self.rows = [dict(episode_id=eid, checkpoint=1000200 + i * 300,
                          direction=contexts[c]["direction"], symbol=contexts[c]["symbol"],
                          phase=contexts[c]["phase"], matching_context=contexts[c])
                     for i, (eid, c) in enumerate(fixture["origins"])]
        membership = {k: [dict(g, context=contexts[g["context"]]) for g in gs]
                      for k, gs in fixture["membership"].items()}
        for name, data in (("frozen-states.json", self.rows), ("membership.json", membership)):
            (self.root / name).write_text(json.dumps(data))
        (self.root / "freeze.json").write_text(json.dumps({"status": "FROZEN",
            "states_sha256": sha(self.root / "frozen-states.json"),
            "membership_sha256": sha(self.root / "membership.json")}))
        self.outcomes = self.base / "outcomes.json"
        self.outcomes.write_text(json.dumps({"records": {r["episode_id"]: {"outcomes": self.horizons(r["checkpoint"] - 300)} for r in self.rows}}))
        self.live = self.base / "research/missed-opportunities.json"
        self.live.parent.mkdir()
        self.records = []
        self.save()

    def horizons(self, stamp, status="COMPLETE"):
        return {h: dict(horizon_status=status, reference_timestamp=stamp,
                       outcome_coverage_complete=status == "COMPLETE", MFE_pct=2, MAE_pct=-1)
                for h in HORIZONS}

    def record(self, i=0, arm="A", complete=True):
        stamp = 2000100 + i * 300
        context = copy.deepcopy(self.rows[0]["matching_context"])
        # Isolate a synthetic matched cell from the historical fixture.
        context["regime"] = "SYNTHETIC_MATCHED"
        eid = f"{context['symbol']}-LONG-MOT-{stamp}-test"
        fields = {f: sorted(a if arm == "A" else b)[0] for f, a, b in ARMS.values()}
        state = dict(schema="smc-hybrid-outcome.v1", episode_id=eid, checkpoint=stamp + 300,
                     symbol=context["symbol"], direction=context["direction"], phase=context["phase"],
                     matching_context=context, fields=fields)
        return dict(tracker_id=eid, symbol=context["symbol"], direction=context["direction"],
                    origin_snapshot_timestamp=stamp, origin_observation=100 + i,
                    snapshots=[dict(snapshot_timestamp=stamp, observation_number=100 + i,
                                    available_at=stamp + 300, smc_state=state)],
                    outcomes=self.horizons(stamp, "COMPLETE" if complete else "PENDING"))

    def save(self):
        self.live.write_text(json.dumps({"schema": "missed-opportunity-tracker.v1", "records": self.records}))

    def status(self):
        self.save()
        return smc_live_sample_status(self.root, self.live, self.outcomes)

    def populate(self, complete=True):
        self.records = [self.record(i, "A" if i < 5 else "B", complete) for i in range(10)]

    def test_historical_counts_unchanged_with_two_unclassified_origins(self):
        self.records = [self.record(i) for i in range(2)]
        for r in self.records:
            del r["snapshots"][0]["smc_state"]
        s = self.status()
        self.assertEqual((s["HISTORICAL_ORIGINS"], s["LIVE_ORIGINS"], s["TOTAL_INDEPENDENT_ORIGINS"]), (45, 2, 47))
        self.assertEqual(s["OUTCOME_COMPLETE_LIVE_ORIGINS"], 2)
        self.assertEqual(s["UNCLASSIFIABLE_LIVE_ORIGINS"], 2)
        expected = {"MSS": (3, 1), "BOS": (1, 3), "FVG_BPR": (2, 0), "PARENT": (3, 1), "OB_BREAKER": (1, 0), "REACTION": (4, 0)}
        for k, v in s["contrasts"].items():
            self.assertEqual(tuple(v["historical"].values()), expected[k])
            self.assertEqual(v["live"], {"A": NA, "B": NA})
            self.assertEqual(v["combined"], {"A": NA, "B": NA})

    def test_checkpoints_are_not_independent_origins(self):
        r = self.record()
        for i in range(1, 100):
            r["snapshots"].append(dict(snapshot_timestamp=3000000 + i * 300, observation_number=200 + i))
        self.records = [r]
        self.assertEqual(self.status()["LIVE_ORIGINS"], 1)

    def test_later_smc_snapshot_not_used(self):
        r = self.record()
        later = copy.deepcopy(r["snapshots"][0])
        later["snapshot_timestamp"] += 300
        later["observation_number"] += 1
        del r["snapshots"][0]["smc_state"]
        r["snapshots"].append(later)
        self.records = [r]
        self.assertEqual(self.status()["CLASSIFIABLE_LIVE_ORIGINS"], 0)

    def test_all_six_contrasts_use_frozen_membership(self):
        self.populate()
        s = self.status()
        for c in s["contrasts"].values():
            self.assertEqual(c["historical"], {"A": 0, "B": 0})
            self.assertEqual(c["live"], {"A": 5, "B": 5})
            self.assertEqual(c["combined"], {"A": 5, "B": 5})
            self.assertIs(c["MATCHED_SAMPLE_READY"], True)
            self.assertIs(c["OUTCOME_READY"], True)
        self.assertEqual(s["NEXT_REVIEW_READY"], {"SAMPLE_READY": True, "OUTCOME_READY": True})

    def test_pending_outcomes_count_for_sample_only(self):
        self.populate(False)
        s = self.status()
        self.assertEqual(s["NEXT_REVIEW_READY"], {"SAMPLE_READY": True, "OUTCOME_READY": False})
        self.assertEqual(s["OUTCOME_COMPLETE_LIVE_ORIGINS"], 0)

    def test_outcomes_cannot_change_membership_or_selected_cell(self):
        self.populate()
        before = self.status()
        for r in self.records:
            r["outcomes"] = self.horizons(r["origin_snapshot_timestamp"], "PENDING")
            r["outcomes"]["1H"]["MFE_pct"] = 9999
        after = self.status()
        for k in ARMS:
            for field in ("context", "historical", "live", "combined", "MATCHED_SAMPLE_READY"):
                self.assertEqual(before["contrasts"][k][field], after["contrasts"][k][field])

    def test_exact_context_not_pooled(self):
        self.populate()
        for r in self.records[5:]:
            r["snapshots"][0]["smc_state"]["matching_context"]["location"] = "OTHER"
        s = self.status()
        self.assertIs(s["NEXT_REVIEW_READY"]["SAMPLE_READY"], False)
        self.assertTrue(all(min(c["live"].values()) == 0 for c in s["contrasts"].values()))

    def test_future_availability_rejected(self):
        r = self.record()
        r["snapshots"][0]["available_at"] += 1
        self.records = [r]
        self.assertEqual(self.status()["CLASSIFIABLE_LIVE_ORIGINS"], 0)

    def test_existing_phase_filter_preserved(self):
        self.populate()
        for r in self.records:
            state = r["snapshots"][0]["smc_state"]
            state["phase"] = state["matching_context"]["phase"] = "UNKNOWN"
        s = self.status()
        self.assertEqual(s["CLASSIFIABLE_LIVE_ORIGINS"], 10)
        self.assertTrue(all(c["live"] == {"A": 0, "B": 0} for c in s["contrasts"].values()))

    def test_composite_reaction_is_not_pooled_into_either_arm(self):
        r = self.record()
        r["snapshots"][0]["smc_state"]["fields"]["reaction"] = "SWEEP_ACCEPTED_OUTSIDE+SWEEP_RECLAIMED"
        self.records = [r]
        s = self.status()
        self.assertEqual(s["CLASSIFIABLE_LIVE_ORIGINS"], 1)
        self.assertEqual(s["contrasts"]["REACTION"]["live"], {"A": 0, "B": 0})

    def test_future_embedded_evidence_rejected(self):
        r = self.record()
        r["snapshots"][0]["smc_state"]["events"] = [{"available_at": r["origin_snapshot_timestamp"] + 301}]
        self.records = [r]
        self.assertEqual(self.status()["live_origins"][0]["reason"], "POST_ORIGIN_SMC_EVIDENCE")

    def test_missing_matching_context_rejected(self):
        r = self.record()
        del r["snapshots"][0]["smc_state"]["matching_context"]["HTF"]
        self.records = [r]
        self.assertEqual(self.status()["CLASSIFIABLE_LIVE_ORIGINS"], 0)

    def test_unknown_smc_label_not_zero_evidence(self):
        r = self.record()
        r["snapshots"][0]["smc_state"]["fields"]["MSS"] = "UNKNOWN"
        self.records = [r]
        self.assertEqual(self.status()["contrasts"]["MSS"]["live"]["A"], NA)

    def test_duplicate_live_record_counted_once(self):
        r = self.record()
        self.records = [r, copy.deepcopy(r)]
        self.assertEqual(self.status()["LIVE_ORIGINS"], 1)

    def test_historical_id_deduplicated(self):
        r = self.record()
        r["tracker_id"] = self.rows[0]["episode_id"].removeprefix("HIST-")
        self.records = [r]
        self.assertEqual(self.status()["LIVE_ORIGINS"], 0)

    def test_historical_origin_identity_deduplicated(self):
        r = self.record()
        h = self.rows[0]
        r.update(symbol=h["symbol"], direction=h["direction"], origin_snapshot_timestamp=h["checkpoint"] - 300)
        self.records = [r]
        self.assertEqual(self.status()["LIVE_ORIGINS"], 0)

    def test_conflicting_duplicate_cannot_supply_membership(self):
        r = self.record()
        other = copy.deepcopy(r)
        other["snapshots"][0]["smc_state"]["fields"]["MSS"] = "GENERIC"
        self.records = [r, other]
        s = self.status()
        self.assertEqual(s["LIVE_ORIGINS"], 1)
        self.assertEqual(s["CLASSIFIABLE_LIVE_ORIGINS"], 0)

    def test_missing_store_is_na(self):
        self.live.unlink()
        s = smc_live_sample_status(self.root, self.live, self.outcomes)
        self.assertEqual(s["LIVE_ORIGINS"], NA)
        self.assertFalse(self.live.exists())

    def test_invalid_store_schema_is_na(self):
        self.live.write_text('{"schema":"wrong","records":[]}')
        self.assertEqual(smc_live_sample_status(self.root, self.live, self.outcomes)["LIVE_ORIGINS"], NA)

    def test_historical_hash_failure_is_na(self):
        (self.root / "membership.json").write_text("{}")
        self.assertEqual(self.status()["contrasts"]["MSS"]["combined"]["A"], NA)

    def test_outcome_reference_mismatch_not_complete(self):
        r = self.record()
        r["outcomes"]["4H"]["reference_timestamp"] += 300
        self.records = [r]
        self.assertEqual(self.status()["OUTCOME_COMPLETE_LIVE_ORIGINS"], 0)

    def test_broken_lifecycle_does_not_remove_origin(self):
        r = self.record()
        r.update(episode_status="BROKEN", status="ACTIVE")
        self.records = [r]
        self.assertEqual(self.status()["CLASSIFIABLE_LIVE_ORIGINS"], 1)

    def test_read_only_all_files_and_runner(self):
        self.populate()
        self.save()
        for n in ("artifact/observation-runner.json", "reviews/thesis-baseline.json", "historical/snapshot.json"):
            p = self.base / n
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{"untouched":true}')
        before = {str(p): sha(p) for p in self.base.rglob("*") if p.is_file()}
        with patch("market_reviewer.cli.run_observation_loop", side_effect=AssertionError("runner")), patch("market_reviewer.cli.prepare_observation", side_effect=AssertionError("prepare")), patch("market_reviewer.cli.observation_runner_status", side_effect=AssertionError("runner status")), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["smc-sample-status", "--root", str(self.root), "--live-store", str(self.live), "--historical-outcomes", str(self.outcomes)]), 0)
        self.assertEqual(before, {str(p): sha(p) for p in self.base.rglob("*") if p.is_file()})

    def test_deterministic_record_order(self):
        self.populate()
        first = self.status()
        self.records.reverse()
        self.assertEqual(first, self.status())

    def test_fresh_process_determinism(self):
        self.populate()
        self.save()
        cmd = [sys.executable, "-B", "-m", "market_reviewer.cli", "smc-sample-status", "--root", str(self.root), "--live-store", str(self.live), "--historical-outcomes", str(self.outcomes)]
        first = subprocess.check_output(cmd, text=True)
        self.assertEqual(first, subprocess.check_output(cmd, text=True))
        self.assertEqual(json.loads(first), self.status())


if __name__ == "__main__":
    unittest.main()
