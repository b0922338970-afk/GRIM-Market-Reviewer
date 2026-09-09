"""Synthetic cluster contracts; no historical outcome labels used."""
import copy
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

from historical_research import liquidity_cluster as lc
from historical_research import parent_attribution as pa
from historical_research import raw_prefix_smc as raw
from market_reviewer.model import Candle
from tests.test_parent_attribution import graph
from tests.test_raw_prefix_smc import frame


def fixture():
    g = graph()
    g["origin_checkpoint"] = 3000
    g["events"]["S"]["evidence"]["candle"] = {"low": 98, "high": 110}
    g["events"]["R"]["evidence"]["close_location"] = "INSIDE"
    return g


def add_sweep(g, name="S2", price=100, low=98, high=110, start=300, tf="M5", scope="External", reaction=True):
    es = g["events"]
    lid = "L" + name
    es[lid] = copy.deepcopy(es["L"])
    es[lid].update(event_id=lid)
    es[lid]["evidence"].update(price=price, type=scope + " Sell-side Liquidity")
    es[name] = copy.deepcopy(es["S"])
    es[name].update(event_id=name, timestamp=start, open_timestamp=start, timeframe=tf, available_at=start+raw.TF[tf])
    es[name]["evidence"].update(level_price=price, sweep_price=low, candle={"low": low, "high": high})
    g["relations"].append(raw.make_relation(es[lid], es[name], "SWEEP_OF", {}, True))
    parent = es[name]
    if reaction:
        rid = "R" + name
        es[rid] = copy.deepcopy(es["R"])
        es[rid].update(event_id=rid, timestamp=es[name]["available_at"], available_at=es[name]["available_at"]+raw.TF[tf], timeframe=tf)
        es[rid]["evidence"].update(level_price=price)
        g["relations"].append(raw.make_relation(es[name], es[rid], "RECLAIM_AFTER", {}, True))
        parent = es[rid]
    proof = dict(g["relations"][2]["proof"], sweep_id=name, liquidity_id=lid)
    g["relations"].append(raw.make_relation(parent, es["D"], "DISPLACEMENT_CANDIDATE", proof))
    return name


def run(g, frames=None):
    raw.ambiguity(g["relations"], g["events"])
    prior = pa.resolve_graph(g, frames or {})
    return lc.reconstruct(g, prior, frames or {})


class LiquidityClusterTests(unittest.TestCase):
    def test_same_zone_overlapping_sweeps_cluster(self):
        g = fixture(); add_sweep(g)
        out = run(g)
        self.assertEqual(len(out["clusters"]), 1)
        self.assertEqual(out["clusters"][0]["member_sweep_ids"], ["S", "S2"])
        self.assertEqual(out["displacements"]["D"]["previous_single_sweep_state"], "AMBIGUOUS_PARENT")
        self.assertEqual(out["displacements"]["D"]["state"], "UNAMBIGUOUS_CLUSTER_PARENT")

    def test_near_time_without_price_geometry_not_clustered(self):
        g = fixture(); add_sweep(g, price=120, low=119, high=125)
        out = run(g)
        self.assertEqual(len(out["clusters"]), 2)
        self.assertEqual(out["displacements"]["D"]["state"], "AMBIGUOUS_CLUSTER_PARENT")

    def test_side_mismatch(self):
        g = fixture(); add_sweep(g)
        g["events"]["S2"]["direction"] = "BEARISH"
        self.assertEqual(len(lc.ClusterBuilder(g, {}).view(900)), 2)

    def test_symbol_mismatch(self):
        g = fixture(); add_sweep(g)
        g["events"]["S2"]["symbol"] = "ETH"
        self.assertEqual(len(lc.ClusterBuilder(g, {}).view(900)), 2)

    def test_same_price_later_separate_cycle(self):
        g = fixture(); add_sweep(g, start=600)
        self.assertEqual(len(lc.ClusterBuilder(g, {}).view(1200)), 2)

    def test_nested_members_preserved(self):
        g = fixture(); add_sweep(g, price=101, low=99, high=106, scope="Internal")
        c = lc.ClusterBuilder(g, {}).view(900)[0]
        self.assertEqual(c["cluster_type"], "NESTED_LIQUIDITY_CLUSTER")
        self.assertEqual(c["nested_depth"], {"S": 0, "S2": 1})

    def test_cross_tf_same_interaction(self):
        g = fixture(); add_sweep(g, tf="M15", reaction=False)
        b = lc.ClusterBuilder(g, {})
        self.assertEqual(len(b.view(1200)), 1)
        self.assertEqual(b.view(600)[0]["member_sweep_ids"], ["S"])

    def test_no_transitive_bridge_merge(self):
        g = fixture()
        g["events"]["S"].update(available_at=1200, timeframe="M15")
        add_sweep(g, "S2", start=900, tf="M15", reaction=False)
        add_sweep(g, "S3", start=1500, tf="M15", reaction=False)
        cs = lc.ClusterBuilder(g, {}).view(2400)
        self.assertEqual(len(cs), 3)
        self.assertTrue(all(c["unresolved_membership_alternatives"] for c in cs))

    def test_context_break_prevents_membership(self):
        g = fixture(); add_sweep(g, tf="M15", reaction=False)
        f = frame(); f.candles = [Candle(600, 100, 101, 95, 96, 1)]
        b = lc.ClusterBuilder(g, {"M5": f})
        self.assertEqual(len(b.view(1200)), 2)
        self.assertIn("CONTEXT_BREAK", b.compatible(g["events"]["S"], g["events"]["S2"])["reasons"])

    def test_swept_not_resolved(self):
        c = lc.ClusterBuilder(fixture(), {}).view(600)[0]
        self.assertEqual(c["lifecycle_state"], "SWEPT")
        self.assertEqual(c["resolution"], "UNRESOLVED")

    def test_reclaim_resolution(self):
        c = lc.ClusterBuilder(fixture(), {}).view(900)[0]
        self.assertEqual(c["resolution"], "RECLAIM_RESOLUTION")
        self.assertEqual(c["resolution_available_at"], 900)

    def test_rejection_resolution(self):
        g = fixture(); g["events"]["R"]["kind"] = "REJECTION"
        g["relations"][1]["type"] = "REJECTION_AFTER"
        self.assertEqual(lc.ClusterBuilder(g, {}).view(900)[0]["resolution"], "REJECTION_RESOLUTION")

    def test_late_raw_reclaim_not_a_cluster_member(self):
        g = fixture()
        g["events"]["R"].update(timestamp=2400, available_at=2700)
        c = lc.ClusterBuilder(g, {}).view(3000)[0]
        self.assertEqual(c["reaction_ids"], [])
        self.assertEqual(c["excluded_reaction_ids"], ["R"])
        self.assertEqual(c["resolution"], "UNRESOLVED")

    def test_acceptance_only_at_existing_deadline(self):
        g = fixture(); g["relations"] = [r for r in g["relations"] if r["type"] != "RECLAIM_AFTER"]
        f = frame(); f.candles = [Candle(1800, 100, 101, 98, 99, 1)]
        b = lc.ClusterBuilder(g, {"M5": f})
        self.assertEqual(b.view(1800)[0]["resolution"], "UNRESOLVED")
        self.assertEqual(b.view(2100)[0]["resolution"], "ACCEPTANCE_RESOLUTION")

    def test_later_context_break_does_not_rewrite_earlier_view(self):
        g = fixture(); f = frame(); f.candles = [Candle(900, 100, 101, 98, 99, 1)]
        b = lc.ClusterBuilder(g, {"M5": f})
        before = copy.deepcopy(b.view(900))
        self.assertEqual(b.view(1200)[0]["lifecycle_state"], "INVALIDATED")
        self.assertEqual(b.view(900), before)

    def test_displacement_uses_only_known_cluster_members(self):
        g = fixture(); add_sweep(g, tf="M15", reaction=False)
        out = run(g)
        vid = out["displacements"]["D"]["resolved_cluster_view_id"]
        self.assertEqual(out["parent_cluster_views"][vid]["member_sweep_ids"], ["S"])

    def test_mss_after_unambiguous_cluster(self):
        g = fixture(); add_sweep(g)
        out = run(g)
        self.assertEqual(out["structure_consequences"][0]["classification"], "CONTEXTUAL_MSS_AFTER_CLUSTER")

    def test_mss_not_promoted_if_two_clusters(self):
        g = fixture(); add_sweep(g, price=120, low=119, high=125)
        self.assertEqual(run(g)["structure_consequences"][0]["classification"], "AMBIGUOUS_CONTEXTUAL_CANDIDATE")

    def test_reversal_chain_requires_resolution_and_zone(self):
        out = run(fixture())
        self.assertEqual(out["chains"][0]["profile"], "REVERSAL")
        self.assertEqual(out["chains"][0]["state"], "UNAMBIGUOUS_CHAIN_CANDIDATE")

    def test_continuation_profile(self):
        g = fixture(); g["events"]["L"]["evidence"]["type"] = "Internal Sell-side Liquidity"
        g["events"]["M"]["kind"] = "BOS"
        out = run(g)
        self.assertEqual(out["chains"][0]["profile"], "CONTINUATION")
        self.assertEqual(out["structure_consequences"][0]["classification"], "CONTEXTUAL_BOS_AFTER_CLUSTER")

    def test_future_origin_event_rejected(self):
        g = fixture(); g["events"]["D"]["available_at"] = 3001
        with self.assertRaises(ValueError): lc.ClusterBuilder(g, {})

    def test_open_candle_excluded(self):
        f = frame(); f.candles = [Candle(900, 100, 101, 95, 96, 1)]
        b = lc.ClusterBuilder(fixture(), {"M5": f})
        self.assertFalse(b.breaks(b.events["S"], 1100))

    def test_no_outcome_access(self):
        g = fixture()
        class Forbidden:
            def __getitem__(self, key): raise AssertionError("outcome access")
        g["outcomes"] = Forbidden()
        with patch("builtins.open", side_effect=AssertionError("unexpected IO")):
            out = run(g)
        self.assertFalse(out["outcome_access"])

    def test_inputs_immutable(self):
        g = fixture(); f = frame(); oldg = copy.deepcopy(g); oldf = copy.deepcopy(f)
        prior = pa.resolve_graph(g, {"M5": f}); oldp = copy.deepcopy(prior)
        lc.reconstruct(g, prior, {"M5": f})
        self.assertEqual(g, oldg); self.assertEqual(f, oldf); self.assertEqual(prior, oldp)

    def test_event_order_does_not_change_ids(self):
        g = fixture(); add_sweep(g)
        other = copy.deepcopy(g); other["events"] = dict(reversed(list(other["events"].items())))
        self.assertEqual(run(g), run(other))

    def test_fresh_process_determinism(self):
        code = "import json; from tests.test_liquidity_cluster import fixture,run,add_sweep; g=fixture(); add_sweep(g); print(json.dumps(run(g),sort_keys=True))"
        results = [subprocess.check_output([sys.executable, "-c", code], env=dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1")) for seed in ("11", "97")]
        self.assertEqual(*results)

    def test_no_valid_route_cannot_gain_cluster_parent(self):
        g = fixture(); g["relations"][2]["proof"]["price_local"] = False
        self.assertEqual(run(g)["displacements"]["D"]["state"], "NO_CLUSTER_PARENT")

    def test_mixed_reference_composition(self):
        g = fixture(); add_sweep(g, scope="Internal")
        c = lc.ClusterBuilder(g, {}).view(900)[0]
        self.assertEqual(c["cluster_type"], "MIXED_LIQUIDITY_CLUSTER")
        self.assertEqual(c["liquidity_side"], "SELLSIDE")
        self.assertEqual(c["reference_composition"], ["EXTERNAL", "INTERNAL", "SWING_LOW"])


if __name__ == "__main__":
    unittest.main()
