"""Hybrid authority contracts, isolated from production and future outcomes."""
import copy
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from historical_research import hybrid_attribution as h
from historical_research import raw_prefix_smc as raw
from historical_research import parent_attribution as pa
from historical_research import liquidity_cluster as lc
from tests.test_liquidity_cluster import fixture, add_sweep
from tests.test_parent_attribution import explicit


def inputs(two=False, separated=False):
    g = fixture()
    if two:
        add_sweep(g, **({"price": 120, "low": 119, "high": 125} if separated else {}))
    raw.ambiguity(g["relations"], g["events"])
    s = pa.resolve_graph(g, {})
    c = lc.reconstruct(g, s, {})
    return g, s, c


def views(c):
    return list(c["parent_cluster_views"].values())


class HybridAttributionTests(unittest.TestCase):
    def test_single_preserved(self):
        p = h.resolve(*inputs())["parents"]["D"]
        self.assertEqual(p["authority_type"], "UNAMBIGUOUS_SINGLE_EVENT_PARENT")
        self.assertEqual(p["single_parent_event_id"], "S")

    def test_unresolved_membership_not_parent_ambiguity(self):
        g, s, c = inputs()
        views(c)[0]["unresolved_membership_alternatives"] = ["background"]
        p = h.resolve(g, s, c)["parents"]["D"]
        self.assertEqual(p["authority_type"], "UNAMBIGUOUS_SINGLE_EVENT_PARENT")
        self.assertEqual(p["cluster_membership_state"], "UNRESOLVED")

    def test_coherent_cluster_escalation(self):
        self.assertEqual(h.resolve(*inputs(True))["parents"]["D"]["authority_type"], "COHERENT_CLUSTER_PARENT")

    def test_multiple_clusters_remain_ambiguous(self):
        self.assertEqual(h.resolve(*inputs(True, True))["parents"]["D"]["authority_type"], "AMBIGUOUS_CLUSTER_PARENTS")

    def test_divergent_reaction_blocks_grouping(self):
        g, s, c = inputs(True)
        views(c)[0]["member_resolution_states"][1]["state"] = "SWEEP_ACCEPTED_OUTSIDE"
        p = h.resolve(g, s, c)["parents"]["D"]
        self.assertEqual(p["authority_type"], "AMBIGUOUS_SINGLE_EVENT_PARENTS")
        self.assertEqual(p["cluster_membership_state"], "REACTION_DIVERGENCE")

    def test_cluster_has_no_winner(self):
        p = h.resolve(*inputs(True))["parents"]["D"]
        self.assertIsNone(p["single_parent_event_id"])
        self.assertIsNotNone(p["cluster_parent_id"])

    def test_all_cluster_members_retained(self):
        self.assertEqual(h.resolve(*inputs(True))["parents"]["D"]["member_parent_event_ids"], ["S", "S2"])

    def test_contextual_mss_inherits(self):
        self.assertEqual(h.resolve(*inputs(True))["structure_consequences"][0]["classification"], "CONTEXTUAL_MSS")

    def test_contextual_bos_inherits(self):
        g, s, c = inputs(True)
        g["events"]["M"]["kind"] = "BOS"
        self.assertEqual(h.resolve(g, s, c)["structure_consequences"][0]["classification"], "CONTEXTUAL_BOS")

    def test_ambiguous_parent_not_contextual_mss(self):
        self.assertEqual(h.resolve(*inputs(True, True))["structure_consequences"][0]["classification"], "AMBIGUOUS_CONTEXTUAL_MSS")

    def test_reversal_chain_cluster(self):
        self.assertEqual(h.resolve(*inputs(True))["chains"][0]["state"], "UNAMBIGUOUS")

    def test_continuation_single(self):
        g, s, c = inputs()
        g["chains"][0]["chain_type"] = "CONTINUATION"
        g["events"]["M"]["kind"] = "BOS"
        self.assertEqual(h.resolve(g, s, c)["chains"][0]["state"], "UNAMBIGUOUS")

    def test_continuation_cluster(self):
        g, s, c = inputs(True)
        g["chains"][0]["chain_type"] = "CONTINUATION"
        g["events"]["M"]["kind"] = "BOS"
        self.assertEqual(h.resolve(g, s, c)["chains"][0]["state"], "UNAMBIGUOUS")

    def test_direct_context_break_invalidates(self):
        g, s, c = inputs()
        views(c)[0]["context_breaks"] = [{"sweep_id": "S", "available_at": 900, "reason": "OPPOSING_DISPLACEMENT"}]
        self.assertEqual(h.resolve(g, s, c)["parents"]["D"]["authority_type"], "NO_VALID_PARENT")

    def test_other_member_break_does_not_invalidate_single(self):
        g, s, c = inputs()
        views(c)[0]["context_breaks"] = [{"sweep_id": "other", "available_at": 900, "reason": "OPPOSING_DISPLACEMENT"}]
        self.assertEqual(h.resolve(g, s, c)["parents"]["D"]["authority_type"], "UNAMBIGUOUS_SINGLE_EVENT_PARENT")

    def test_no_transitive_membership_merge(self):
        g, s, c = inputs(True)
        views(c)[0]["membership_proofs"] = []
        self.assertEqual(h.resolve(g, s, c)["parents"]["D"]["authority_type"], "AMBIGUOUS_SINGLE_EVENT_PARENTS")

    def test_268_historical_regressions(self):
        from historical_research.run_hybrid_attribution import OUT, read
        if not (OUT / "cohorts.json").exists():
            self.skipTest("optional frozen 45-origin runtime corpus unavailable")
        rows = read(OUT / "cohorts.json")["regressions"]
        self.assertEqual(len(rows), 268)
        self.assertTrue(all(r["regression_result"] == "PRESERVED" for r in rows))

    def test_161_historical_groupings(self):
        from historical_research.run_hybrid_attribution import OUT, read
        if not (OUT / "cohorts.json").exists():
            self.skipTest("optional frozen 45-origin runtime corpus unavailable")
        rows = read(OUT / "cohorts.json")["groupings"]
        self.assertEqual(len(rows), 161)
        self.assertTrue(all(r["grouping_result"] == "ESCALATED" for r in rows))

    def test_deterministic_identity(self):
        args = inputs(True)
        self.assertEqual(h.resolve(*args), h.resolve(*copy.deepcopy(args)))

    def test_no_outcome_access(self):
        class NoOutcome(dict):
            def __getitem__(self, key):
                if key in {"outcomes", "snapshots", "mfe", "mae"}:
                    raise AssertionError("outcome accessed")
                return super().__getitem__(key)
        g, s, c = inputs()
        self.assertFalse(h.resolve(NoOutcome(g), s, c)["outcome_access"])

    def test_snapshot_immutability(self):
        args = inputs(True)
        before = copy.deepcopy(args)
        h.resolve(*args)
        self.assertEqual(args, before)

    def test_production_isolation(self):
        args = inputs()
        with patch("market_reviewer.persistence.atomic_write_json", side_effect=AssertionError("production write")), patch("builtins.open", side_effect=AssertionError("IO")):
            h.resolve(*args)

    def test_fresh_process(self):
        code = "from tests.test_hybrid_attribution import inputs; from historical_research.hybrid_attribution import resolve; import json; print(json.dumps(resolve(*inputs(True)),sort_keys=True))"
        a = subprocess.check_output([sys.executable, "-c", code], env=dict(os.environ, PYTHONHASHSEED="1"))
        b = subprocess.check_output([sys.executable, "-c", code], env=dict(os.environ, PYTHONHASHSEED="91"))
        self.assertEqual(a, b)

    def test_post_checkpoint_rejected(self):
        g, s, c = inputs()
        g["events"]["D"]["available_at"] = g["origin_checkpoint"] + 1
        with self.assertRaises(ValueError):
            h.resolve(g, s, c)

    def test_temporal_order_contradiction(self):
        g, s, c = inputs()
        g["events"]["S"]["available_at"] = g["events"]["D"]["timestamp"] + 1
        self.assertEqual(h.resolve(g, s, c)["parents"]["D"]["authority_type"], "NO_VALID_PARENT")

    def test_direction_contradiction(self):
        g, s, c = inputs()
        g["events"]["S"]["direction"] = "BEARISH"
        self.assertEqual(h.resolve(g, s, c)["parents"]["D"]["authority_type"], "NO_VALID_PARENT")

    def test_exact_parent_retained(self):
        g = fixture()
        explicit(g, "S")
        s = pa.resolve_graph(g, {})
        c = lc.reconstruct(g, s, {})
        self.assertEqual(h.resolve(g, s, c)["parents"]["D"]["authority_type"], "EXACT_SINGLE_EVENT_PARENT")

    def test_unavailable_reaction_not_grouping_veto(self):
        g, s, c = inputs(True)
        views(c)[0]["resolution"] = "UNRESOLVED"
        views(c)[0]["member_resolution_states"] = []
        self.assertEqual(h.resolve(g, s, c)["parents"]["D"]["authority_type"], "COHERENT_CLUSTER_PARENT")

    def test_missing_shared_structure_not_grouping_veto(self):
        g, s, c = inputs(True)
        g["relations"] = [r for r in g["relations"] if r["type"] != "STRUCTURE_CONSEQUENCE_CANDIDATE"]
        p = h.resolve(g, s, c)
        self.assertEqual(p["parents"]["D"]["authority_type"], "COHERENT_CLUSTER_PARENT")
        self.assertEqual(p["chains"][0]["state"], "PARTIAL")

    def test_ambiguous_structure_stays_ambiguous(self):
        g, s, c = inputs(True)
        edge = copy.deepcopy(next(r for r in g["relations"] if r["type"] == "STRUCTURE_CONSEQUENCE_CANDIDATE"))
        edge.update(parent="D2", relation_id="different")
        g["relations"].append(edge)
        self.assertEqual(h.resolve(g, s, c)["structure_consequences"][0]["classification"], "AMBIGUOUS_CONTEXTUAL_MSS")


if __name__ == "__main__":
    unittest.main()
