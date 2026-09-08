"""Parent attribution proof contracts; synthetic cases are not historical claims."""
import copy
import json
import os
import subprocess
import sys
import unittest
from unittest.mock import patch
from market_reviewer.model import Candle
from historical_research import parent_attribution as pa
from historical_research import raw_prefix_smc as raw
from tests.test_raw_prefix_smc import fixture, ev, frame


def graph(two=False):
    es,rs=fixture()
    rs[2]["proof"].update(liquidity_id="L",price_local=True,confirmed_reference={"price":105},structure_penetrating=True,context_break=[])
    rs[3]["proof"].update(reference=105)
    if two:
        s2=copy.deepcopy(es["S"]);s2.update(event_id="S2",timestamp=600,available_at=900)
        es["S2"]=s2
        rs.append(raw.make_relation(es["L"],s2,"SWEEP_OF",{},True))
        rs.append(raw.make_relation(s2,es["D"],"DISPLACEMENT_CANDIDATE",dict(rs[2]["proof"],sweep_id="S2")))
    raw.ambiguity(rs,es)
    return {"origin_checkpoint":100000,"episode_id":"test","events":es,"relations":rs,
            "contextual_mss_candidates":raw.contextual_candidates(es,rs),"chains":raw.chain_candidates(es,rs,{"events":es})}


def result(g,frames=None):
    return pa.resolve_graph(g,frames or {})["displacements"]["D"]


def explicit(g,parent):
    g["explicit_parent_identities"]=[{"displacement_id":"D","sweep_id":parent,"source_event_id":"M",
                                    "proof_type":"EXPLICIT_DETECTOR_REFERENCE","available_at":1200}]


class ParentAttributionTests(unittest.TestCase):
    def test_01_nearest_does_not_win(self):
        r=result(graph(True));self.assertEqual(r["resolution_state"],"AMBIGUOUS_PARENT")
        self.assertIsNone(r["resolved_parent_id"])

    def test_02_supersession_requires_observed_break(self):
        g=graph(True);es=g["events"];es["D"].update(timestamp=1800,available_at=2100)
        es["S2"].update(timestamp=1200,available_at=1500)
        rec=ev("RECLAIMED",1500,"R2",level_price=100);es["R2"]=rec
        g["relations"]=[r for r in g["relations"] if not(r["parent"]=="S2" and r["type"]=="DISPLACEMENT_CANDIDATE")]
        g["relations"].extend([raw.make_relation(es["S2"],rec,"RECLAIM_AFTER",{},True),
            raw.make_relation(rec,es["D"],"DISPLACEMENT_CANDIDATE",dict(g["relations"][2]["proof"],sweep_id="S2"))])
        f=frame(8);f.candles=[Candle(900,100,101,98,99,1),Candle(1200,99,101,98,99,1),Candle(1500,99,102,99,101,1)]
        r=result(g,{"M5":f})
        self.assertEqual(r["resolved_parent_id"],"S2")
        self.assertEqual(next(p for p in r["parents"] if p["sweep_id"]=="S")["supersession_state"],"SUPERSEDED_PARENT")

    def test_03_nested_preserved(self):
        g=graph(True);g["events"]["L2"]=ev("LIQUIDITY",0,"L2",price=101,type="Internal Sell-side Liquidity")
        g["relations"][-1]["proof"]["liquidity_id"]="L2"
        g["events"]["S"]["evidence"]["candle"]={"low":98,"high":110}
        g["events"]["S2"]["evidence"]["candle"]={"low":100,"high":106}
        r=result(g);self.assertEqual(r["resolution_state"],"AMBIGUOUS_PARENT")
        self.assertIn("NESTED_SWEEPS",r["ambiguity_causes"])

    def test_04_external_reversal_not_universal_rank(self):
        r=result(graph());self.assertEqual(r["parents"][0]["routes"][0]["factors"]["LIQUIDITY_RELEVANCE"]["evidence"]["candidate_context"],"REVERSAL")

    def test_05_internal_continuation_valid(self):
        g=graph();g["events"]["L"]["evidence"]["type"]="Internal Sell-side Liquidity"
        self.assertEqual(result(g)["resolution_state"],"UNAMBIGUOUS_PARENT")

    def test_06_tf_bridge(self):
        g=graph();g["events"]["L"]["timeframe"]="H1";g["events"]["S"]["timeframe"]="M15"
        f=result(g)["parents"][0]["routes"][0]["factors"]
        self.assertEqual(f["TIMEFRAME_COMPATIBILITY"]["state"],pa.SUPPORT)

    def test_07_unrelated_price_rejected(self):
        g=graph();g["relations"][2]["proof"]["price_local"]=False
        self.assertEqual(result(g)["resolution_state"],"NO_VALID_PARENT")

    def test_08_context_break(self):
        g=graph();g["relations"][2]["proof"]["context_break"]=["PROTECTED_LEVEL_LOSS"]
        r=result(g)
        self.assertEqual(r["resolution_state"],"NO_VALID_PARENT")
        self.assertNotEqual(r["parents"][0]["supersession_state"],"ACTIVE_PARENT")

    def test_09_accepted_outside_rejected(self):
        g=graph();g["events"]["D"].update(timestamp=1500,available_at=1800)
        f=frame(6);f.candles=[Candle(900,101,102,98,99,1)]
        r=result(g,{"M5":f});self.assertEqual(r["resolution_state"],"NO_VALID_PARENT")
        self.assertEqual(r["parents"][0]["routes"][0]["factors"]["RECLAIM_CONTINUITY"]["state"],pa.CONTRADICT)

    def test_10_explicit_structure_identity_resolves(self):
        g=graph(True);explicit(g,"S")
        r=result(g);self.assertEqual(r["resolution_state"],"EXACT_PARENT")
        self.assertEqual(r["resolved_parent_id"],"S")

    def test_11_plausible_parents_retained(self):
        r=result(graph(True));self.assertEqual(r["valid_parent_count"],2)
        self.assertEqual(len(r["parents"]),2)

    def test_12_all_contradicted_no_valid(self):
        g=graph(True)
        for r in g["relations"]:
            if r["type"]=="DISPLACEMENT_CANDIDATE":r["proof"]["context_break"]=["LOSS"]
        self.assertEqual(result(g)["resolution_state"],"NO_VALID_PARENT")

    def test_13_mss_inherits_parent_ambiguity(self):
        out=pa.resolve_graph(graph(True),{})
        self.assertEqual(out["contextual_mss"][0]["state"],"AMBIGUOUS")

    def test_14_reversal_chain(self):
        c=pa.resolve_graph(graph(),{})["chains"][0]
        self.assertEqual(c["chain_type"],"REVERSAL");self.assertEqual(c["state"],"UNAMBIGUOUS")

    def test_15_continuation_chain(self):
        g=graph();g["events"]["L"]["evidence"]["type"]="Internal Sell-side Liquidity";g["events"]["M"]["kind"]="BOS"
        g["chains"]=raw.chain_candidates(g["events"],g["relations"],g)
        c=pa.resolve_graph(g,{})["chains"][0]
        self.assertEqual(c["chain_type"],"CONTINUATION");self.assertEqual(c["state"],"UNAMBIGUOUS")

    def test_16_no_outcome_or_file_access(self):
        g=graph()
        class Forbidden:
            def __getitem__(self,k):raise AssertionError("outcome accessed")
        g["outcomes"]=Forbidden()
        with patch("builtins.open",side_effect=AssertionError("file access")):
            r=result(g)
        self.assertEqual(r["resolution_state"],"UNAMBIGUOUS_PARENT")

    def test_17_deterministic_ids(self):
        self.assertEqual(result(graph()),result(graph()))

    def test_18_snapshot_immutable(self):
        g=graph(True);before=copy.deepcopy(g);result(g);self.assertEqual(g,before)

    def test_19_production_isolation(self):
        f=frame();before=copy.deepcopy(f);result(graph(),{"M5":f});self.assertEqual(f,before)

    def test_20_fresh_process_determinism(self):
        script="import json; from tests.test_parent_attribution import graph,result; print(json.dumps(result(graph(True)),sort_keys=True))"
        rows=[subprocess.check_output([sys.executable,"-c",script],env=dict(os.environ,PYTHONHASHSEED=s,PYTHONDONTWRITEBYTECODE="1")) for s in ("11","97")]
        self.assertEqual(rows[0],rows[1])

    def test_21_future_identity_cannot_resolve(self):
        g=graph(True);explicit(g,"S");g["explicit_parent_identities"][0]["available_at"]=100001
        self.assertEqual(result(g)["resolution_state"],"AMBIGUOUS_PARENT")

    def test_22_mismatching_generic_reference_not_exclusion(self):
        g=graph(True);g["relations"][-1]["proof"]["confirmed_reference"]={"price":200}
        self.assertEqual(result(g)["resolution_state"],"AMBIGUOUS_PARENT")

    def test_23_parent_candle_overlapping_displacement_not_context_break(self):
        ix=pa.ClosedIndex({"M5":frame()},2000)
        self.assertEqual(ix.between("M5",900,1100),[])

    def test_24_all_factors_present_without_scores(self):
        r=result(graph());f=r["parents"][0]["routes"][0]["factors"]
        self.assertEqual(set(f),set(pa.FACTORS))
        self.assertTrue(all(x["state"] in {pa.SUPPORT,pa.NEUTRAL,pa.CONTRADICT,pa.UNAVAILABLE} for x in f.values()))

    def test_25_no_universal_external_ranking(self):
        g=graph(True);g["events"]["L2"]=ev("LIQUIDITY",0,"L2",price=101,type="Internal Sell-side Liquidity")
        g["relations"][-1]["proof"]["liquidity_id"]="L2"
        self.assertEqual(result(g)["resolution_state"],"AMBIGUOUS_PARENT")

    def test_26_unrelated_explicit_source_not_proof(self):
        g=graph(True);explicit(g,"S");g["explicit_parent_identities"][0]["source_event_id"]="Z"
        self.assertEqual(result(g)["resolution_state"],"AMBIGUOUS_PARENT")


if __name__=="__main__":unittest.main()
