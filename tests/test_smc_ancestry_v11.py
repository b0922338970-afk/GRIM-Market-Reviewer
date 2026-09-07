"""Deterministic offline ancestry contracts, independent of local research artifacts."""
import copy
import tempfile
import unittest
from pathlib import Path
from market_reviewer.model import Candle
from historical_research.smc_ancestry_v11 import (
    build_chains, contextual_kind, deadline, identity, qualify, reconstruct, relation,
    sweep_state, validate_reaction, validate_sweep,
)


def event(kind, ts, key=None, **evidence):
    return {"event_id": key or kind, "kind":kind, "symbol":"BTC", "direction":"BULLISH",
            "timeframe":"M5", "timestamp":ts, "available_at":ts+300, "evidence":evidence}


def fixture(internal=False):
    typ = "Internal Sell-side Liquidity" if internal else "External Sell-side Liquidity"
    return (
        event("LIQUIDITY",0,price=100,type=typ),
        event("SWEPT",300,level_price=100,level_type=typ,penetration=2,sweep_price=98),
        event("RECLAIMED",600,level_price=100,close_location="INSIDE"),
        event("DISPLACEMENT",900),
    )


def proof():
    return {"reference_known_before_sweep":True, "level_price_interaction":True,
            "meaningful_structure_penetration":True, "generic_break_matches_reference":True,
            "context_break":[]}


def graph(continuation=False, zone=True):
    l,s,r,d = fixture(continuation)
    c = event("CONTEXTUAL_BOS_CONTINUATION" if continuation else "CONTEXTUAL_MSS",900)
    es = {e["event_id"]:e for e in [l,s,r,d,c]}; rs={}
    for p,ch,k in [(l,s,"SWEEP_OF"),(s,r,"RECLAIM_AFTER"),(s,d,"DISPLACEMENT_AFTER_SWEEP"),
                   (r,d,"DISPLACEMENT_AFTER_RECLAIM"),(d,c,"CONTEXTUAL_STRUCTURE")]:
        rel=relation(p,ch,k,"synthetic exact proof",proof());rs[rel["relationship_id"]]=rel
    if zone:
        z=event("FVG",1200);es[z["event_id"]]=z
        rel=relation(d,z,"FVG_CREATED_BY","exact ancestor",{});rs[rel["relationship_id"]]=rel
    return es,rs


class SMCAncestryV11Tests(unittest.TestCase):
    def test_01_exact_liquidity_sweep(self):
        l,s,_,_=fixture();self.assertTrue(validate_sweep(l,s))
    def test_02_wrong_liquidity_rejected(self):
        l,s,_,_=fixture();l["evidence"]["price"]=101;self.assertFalse(validate_sweep(l,s))
    def test_03_reclaim_after_sweep(self):
        _,s,r,_=fixture();self.assertTrue(validate_reaction(s,r))
        r["timestamp"]=s["timestamp"];self.assertFalse(validate_reaction(s,r))
    def test_04_acceptance_outside(self):
        _,s,_,_=fixture();end=deadline(s);c=Candle(end-300,99,100,97,99,0)
        self.assertEqual(sweep_state(s,[],[c],end)[0],"SWEEP_ACCEPTED_OUTSIDE")
        self.assertEqual(sweep_state(s,[],[c],end-1)[0],"SWEEP_UNRESOLVED")
    def test_05_sweep_linked_displacement(self):
        _,s,_,d=fixture();self.assertEqual(qualify(s,d,None,proof()),"PASS")
    def test_06_unrelated_displacement_rejected(self):
        _,s,_,d=fixture();p=proof();p["level_price_interaction"]=False
        self.assertEqual(qualify(s,d,None,p),"UNRELATED_PRICE_ACTION")
    def test_07_reclaim_linked_displacement(self):
        _,s,r,d=fixture();self.assertEqual(qualify(s,d,r,proof()),"PASS")
    def test_08_causal_window_expiration(self):
        _,s,_,d=fixture();d["available_at"]=deadline(s)+1
        self.assertEqual(qualify(s,d,None,proof()),"CAUSAL_WINDOW_EXPIRED")
    def test_09_context_break_blocks_link(self):
        _,s,r,d=fixture();p=proof();p["context_break"]=["PROTECTED_LOSS"]
        self.assertEqual(qualify(s,d,r,p),"CONTEXT_BREAK")
    def test_10_generic_mss_without_ancestry(self):
        m=event("MSS",300);v={"origin_checkpoint":600,"events":{"MSS":m},"relations":{},
          "chains":[],"phase":"PULLBACK","direction":"LONG","episode_id":"fixture"}
        out=reconstruct(v,{})
        self.assertEqual(out["events"]["MSS"]["kind"],"MSS")
        self.assertFalse(any(e["kind"]=="CONTEXTUAL_MSS" for e in out["events"].values()))
    def test_11_contextual_mss_full_ancestry(self):
        l,s,r,d=fixture()
        self.assertEqual(qualify(s,d,r,proof()),"PASS")
        self.assertEqual(contextual_kind(l,"MSS",r,"REVERSAL_CANDIDATE"),"CONTEXTUAL_MSS")
        self.assertIsNone(contextual_kind(l,"MSS",None,"REVERSAL_CANDIDATE"))
    def test_12_continuation_bos(self):
        l,_,_,_=fixture(True)
        self.assertEqual(contextual_kind(l,"BOS",None,"CONTINUATION"),"CONTEXTUAL_BOS_CONTINUATION")
    def test_13_fvg_inherits_ancestry(self):
        es,rs=graph();c=build_chains(es,rs,1800,[])[0]
        self.assertEqual(c["ancestry_depth"],"L5");self.assertIn("FVG",c["event_ids"])
    def test_14_ob_inherits_ancestry(self):
        es,rs=graph(zone=False);ob=event("OB",600);ob["available_at"]=1200;es["OB"]=ob
        r=relation(es["DISPLACEMENT"],ob,"OB_ASSOCIATED_WITH","confirmed at D",{})
        rs[r["relationship_id"]]=r
        self.assertEqual(build_chains(es,rs,1800,[])[0]["ancestry_depth"],"L5")
    def test_15_reversal_chain(self):
        es,rs=graph();self.assertEqual(build_chains(es,rs,1800,[])[0]["chain_type"],"REVERSAL_CHAIN")
    def test_16_continuation_chain(self):
        es,rs=graph(True);self.assertEqual(build_chains(es,rs,1800,[])[0]["chain_type"],"CONTINUATION_CHAIN")
    def test_17_partial_state(self):
        l,s,_,_=fixture();r=relation(l,s,"SWEEP_OF","exact",{})
        c=build_chains({l["event_id"]:l,s["event_id"]:s},{r["relationship_id"]:r},1000,[])[0]
        self.assertEqual(c["partial_state"],"SWEEP_ONLY")
        self.assertEqual(c["chain_type"],"PARTIAL_CHAIN")
    def test_18_failed_state(self):
        l,s,_,_=fixture();s["evidence"]["aftermath"]="SWEEP_ACCEPTED_OUTSIDE"
        r=relation(l,s,"SWEEP_OF","exact",{})
        c=build_chains({l["event_id"]:l,s["event_id"]:s},{r["relationship_id"]:r},deadline(s),[])[0]
        self.assertEqual(c["chain_type"],"FAILED_CHAIN")
        self.assertIn("SWEEP_ACCEPTED_OUTSIDE",c["failure_states"])
    def test_19_broken_chain_no_revival(self):
        es,rs=graph();b={"anchor_id":"LIQUIDITY","available_at":900,"event_id":"break","reason":"OPPOSITE_STRUCTURE"}
        c=build_chains(es,rs,1800,[b])[0]
        self.assertEqual(c["chain_type"],"FAILED_CHAIN")
        self.assertNotIn("FVG",c["event_ids"])
        self.assertIn("FVG",c["excluded_post_break_event_ids"])
    def test_20_ids_deterministic(self):
        self.assertEqual(identity("L",{"p":1,"t":2}),identity("L",{"t":2,"p":1}))
        es,rs=graph();a=build_chains(es,rs,1800,[]);b=build_chains(dict(reversed(list(es.items()))),rs,1800,[])
        self.assertEqual(a,b)
    def test_21_no_hindsight(self):
        e=event("MSS",600)
        with self.assertRaises(ValueError):
            reconstruct({"origin_checkpoint":600,"events":{"MSS":e}}, {})
    def test_22_live_state_isolation(self):
        with tempfile.TemporaryDirectory() as t:
            p=Path(t)/"production.json";p.write_text('{"locked":true}')
            before=p.read_bytes();es,rs=graph();build_chains(es,rs,1800,[])
            self.assertEqual(before,p.read_bytes())
    def test_23_historical_snapshot_immutable(self):
        m=event("MSS",300);v={"origin_checkpoint":600,"events":{"MSS":m},"relations":{},
          "chains":[],"phase":"PULLBACK","direction":"LONG","episode_id":"fixture"}
        before=copy.deepcopy(v);reconstruct(v,{})
        self.assertEqual(v,before)

    def test_24_internal_mss_not_external_reversal(self):
        es,rs=graph()
        es["LIQUIDITY"]["evidence"]["type"]="Internal Sell-side Liquidity"
        c=build_chains(es,rs,1800,[])[0]
        self.assertEqual(c["chain_type"],"PARTIAL_CHAIN")
        self.assertEqual(c["ancestry_depth"],"L5")

    def test_25_equal_level_contextual_mss_not_invented_external(self):
        l,_,r,_=fixture()
        l["evidence"]["type"]="Equal Lows"
        self.assertEqual(contextual_kind(l,"MSS",r,"PULLBACK"),"CONTEXTUAL_MSS")
        self.assertIsNone(contextual_kind(l,"BOS",r,"CONTINUATION"))

    def test_26_same_candle_availability_violation(self):
        e=event("MSS",600);e["available_at"]=600
        with self.assertRaises(ValueError):
            reconstruct({"origin_checkpoint":900,"events":{"MSS":e}}, {})

    def test_27_other_displacement_zone_not_full_ancestry(self):
        es,rs=graph(zone=False)
        d2=event("DISPLACEMENT",1200,key="D2");z=event("FVG",1500)
        es["D2"]=d2;es["FVG"]=z
        for p,c,k in [(es["SWEPT"],d2,"DISPLACEMENT_AFTER_SWEEP"),(d2,z,"FVG_CREATED_BY")]:
            r=relation(p,c,k,"separate branch",{});rs[r["relationship_id"]]=r
        chain=build_chains(es,rs,2100,[])[0]
        self.assertEqual(chain["ancestry_depth"],"L4")
        self.assertEqual(chain["chain_type"],"PARTIAL_CHAIN")
        self.assertEqual(chain["partial_state"],"MSS_NO_LINKED_IMBALANCE")

    def test_28_missing_window_not_expired_failure(self):
        l,s,_,_=fixture();r=relation(l,s,"SWEEP_OF","exact",{})
        c=build_chains({l["event_id"]:l,s["event_id"]:s},{r["relationship_id"]:r},deadline(s),[])[0]
        self.assertEqual(c["chain_type"],"PARTIAL_CHAIN")

    def test_29_covered_window_allows_no_reclaim_failure(self):
        l,s,_,_=fixture();s["evidence"]["causal_window_covered"]=True
        r=relation(l,s,"SWEEP_OF","exact",{})
        c=build_chains({l["event_id"]:l,s["event_id"]:s},{r["relationship_id"]:r},deadline(s),[])[0]
        self.assertIn("SWEEP_NO_RECLAIM",c["failure_states"])

    def test_30_unlinked_displacement_not_absent_displacement(self):
        l,s,_,_=fixture();s["evidence"].update(causal_window_covered=True,displacement_observed_in_window=True)
        r=relation(l,s,"SWEEP_OF","exact",{})
        c=build_chains({l["event_id"]:l,s["event_id"]:s},{r["relationship_id"]:r},deadline(s),[])[0]
        self.assertEqual(c["chain_type"],"PARTIAL_CHAIN")

    def test_31_future_break_does_not_rewrite_decision(self):
        es,rs=graph()
        future={"anchor_id":"LIQUIDITY","available_at":2100,"event_id":"future","reason":"CHAIN_DEFENSE_FAILURE"}
        self.assertEqual(build_chains(es,rs,1800,[]),build_chains(es,rs,1800,[future]))

    def test_32_future_relationship_rejected(self):
        es,rs=graph();next(iter(rs.values()))["available_at"]=2100
        with self.assertRaises(ValueError):
            build_chains(es,rs,1800,[])
