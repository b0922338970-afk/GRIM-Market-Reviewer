import copy
import hashlib
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
from market_reviewer.smc_sample_status import CONTRASTS, NA, smc_sample_status

FIXTURE = Path(__file__).parent / "fixtures/smc_sample_status.json"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class SMCSampleStatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "research/historical-replay/matched"
        fixture = json.loads(FIXTURE.read_text())
        contexts = fixture["contexts"]
        self.rows = [{"episode_id": eid, "direction": contexts[c]["direction"],
                      "phase": contexts[c]["phase"], "symbol": contexts[c]["symbol"],
                      "matching_context": contexts[c]} for eid, c in fixture["origins"]]
        self.membership = {k: [dict(g, context=contexts[g["context"]]) for g in groups]
                           for k, groups in fixture["membership"].items()}
        self.save()

    def save(self):
        self.root.mkdir(parents=True, exist_ok=True)
        for filename, content in (("frozen-states.json", self.rows), ("membership.json", self.membership)):
            (self.root / filename).write_text(json.dumps(content), encoding="utf-8")
        freeze = {"status": "FROZEN", "states_sha256": sha(self.root / "frozen-states.json"),
                  "membership_sha256": sha(self.root / "membership.json")}
        (self.root / "freeze.json").write_text(json.dumps(freeze), encoding="utf-8")

    def test_existing_counts_reproduced(self):
        s = smc_sample_status(self.root)
        self.assertEqual((s["total_independent_origins"], s["LONG"], s["SHORT"]), (45, 23, 22))
        expected = {"MSS": (3, 1), "BOS": (1, 3), "FVG_BPR": (2, 0),
                    "PARENT": (3, 1), "OB_BREAKER": (1, 0), "REACTION": (4, 0)}
        self.assertEqual({k: (v["A_count"], v["B_count"]) for k, v in s["contrasts"].items()}, expected)
        self.assertEqual(s["READY_CONTRAST_COUNT"], 0)
        self.assertEqual(s["TOTAL_CONTRAST_COUNT"], 6)
        self.assertIs(s["NEXT_REVIEW_READY"], False)

    def test_read_only_runner_production_and_snapshots(self):
        for name in ("artifact/observation-runner.json", "artifact/observation-commit-journal.json",
                     "artifact/production-observation-head.json", "reviews/thesis-baseline.json",
                     "research/missed-opportunities.json", "historical/frozen-origin.json"):
            p = self.base / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{"sentinel":"immutable"}')
        before = {str(p): sha(p) for p in self.base.rglob("*") if p.is_file()}
        with patch("market_reviewer.cli.run_observation_loop", side_effect=AssertionError("runner invoked")), patch("market_reviewer.cli.prepare_observation", side_effect=AssertionError("prepare invoked")), patch("market_reviewer.cli.observation_runner_status", side_effect=AssertionError("runner inspected")), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["smc-sample-status", "--root", str(self.root)]), 0)
        after = {str(p): sha(p) for p in self.base.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_missing_directory_is_na_and_not_created(self):
        root = self.base / "absent"
        s = smc_sample_status(root)
        self.assertFalse(root.exists())
        self.assertEqual(s["total_independent_origins"], NA)
        self.assertEqual(s["READY_CONTRAST_COUNT"], NA)
        self.assertEqual(s["NEXT_REVIEW_READY"], NA)
        self.assertTrue(all(v["A_count"] == NA for v in s["contrasts"].values()))

    def test_missing_arm_is_not_zero(self):
        del self.membership["MSS"][0]["A"]
        self.save()
        self.assertEqual(smc_sample_status(self.root)["contrasts"]["MSS"]["A_count"], NA)

    def test_explicit_empty_arm_is_observed_zero(self):
        c = smc_sample_status(self.root)["contrasts"]["FVG_BPR"]
        self.assertEqual(c["availability"], "AVAILABLE")
        self.assertEqual(c["B_count"], 0)

    def test_hash_mismatch_fails_closed(self):
        with (self.root / "membership.json").open("a") as f:
            f.write(" ")
        s = smc_sample_status(self.root)
        self.assertEqual(s["total_independent_origins"], 45)
        self.assertEqual(s["contrasts"]["MSS"]["READY"], NA)

    def test_corrupt_json_is_na(self):
        (self.root / "freeze.json").write_text("{")
        self.assertEqual(smc_sample_status(self.root)["LONG"], NA)

    def test_missing_contrast_is_na(self):
        del self.membership["MSS"]
        self.save()
        self.assertEqual(smc_sample_status(self.root)["contrasts"]["MSS"]["READY"], NA)

    def test_empty_cell_list_is_na(self):
        self.membership["MSS"] = []
        self.save()
        self.assertEqual(smc_sample_status(self.root)["contrasts"]["MSS"]["context"], NA)

    def test_duplicate_origin_rejected(self):
        self.rows.append(copy.deepcopy(self.rows[0]))
        self.save()
        self.assertEqual(smc_sample_status(self.root)["total_independent_origins"], NA)

    def test_duplicate_member_rejected(self):
        g = next(g for g in self.membership["MSS"] if g["A"])
        g["A"].append(g["A"][0])
        self.save()
        self.assertEqual(smc_sample_status(self.root)["contrasts"]["MSS"]["READY"], NA)

    def test_context_mismatch_rejected(self):
        self.membership["MSS"][0]["context"] = dict(self.membership["MSS"][0]["context"], regime="DIFFERENT")
        self.save()
        self.assertEqual(smc_sample_status(self.root)["contrasts"]["MSS"]["READY"], NA)

    def test_no_cross_cell_pooling(self):
        s = smc_sample_status(self.root)
        self.assertGreater(sum(len(g["A"]) for g in self.membership["MSS"]), 5)
        self.assertIs(s["contrasts"]["MSS"]["READY"], False)

    def ready_cell(self):
        context = copy.deepcopy(self.rows[0]["matching_context"])
        ids = [f"synthetic-{i}" for i in range(10)]
        self.rows = [dict(episode_id=eid, direction=context["direction"], symbol=context["symbol"], phase=context["phase"], matching_context=context) for eid in ids]
        return {"context": context, "A": ids[:5], "B": ids[5:]}

    def test_two_priority_contrasts_trigger_monitor_flag(self):
        g = self.ready_cell()
        self.membership = {k: [copy.deepcopy(g)] for k in CONTRASTS}
        for k in list(CONTRASTS)[2:]:
            self.membership[k][0]["A"] = g["A"][:4]
        self.save()
        s = smc_sample_status(self.root)
        self.assertEqual(s["READY_CONTRAST_COUNT"], 2)
        self.assertIs(s["NEXT_REVIEW_READY"], True)
        self.assertEqual(s["contrasts"]["MSS"]["preferred_per_side"], [8, 10])

    def test_one_contrast_not_enough(self):
        g = self.ready_cell()
        self.membership = {k: [dict(g, A=g["A"][:4])] for k in CONTRASTS}
        self.membership["MSS"] = [g]
        self.save()
        self.assertIs(smc_sample_status(self.root)["NEXT_REVIEW_READY"], False)

    def test_deterministic_order(self):
        expected = smc_sample_status(self.root)
        self.rows.reverse()
        self.membership = {k: list(reversed(gs)) for k, gs in reversed(list(self.membership.items()))}
        self.save()
        self.assertEqual(expected, smc_sample_status(self.root))

    def test_fresh_process_output_deterministic(self):
        cmd = [sys.executable, "-B", "-m", "market_reviewer.cli", "smc-sample-status", "--root", str(self.root)]
        first = subprocess.check_output(cmd, text=True)
        second = subprocess.check_output(cmd, text=True)
        self.assertEqual(first, second)
        self.assertEqual(json.loads(first), smc_sample_status(self.root))


if __name__ == "__main__":
    unittest.main()
