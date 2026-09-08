"""Offline candidate-only contracts; no historical runtime data required."""
import copy
import inspect
import json
import os
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from market_reviewer.model import Candle, MarketDataFrame
from market_reviewer import reviewer as rv
from historical_research import raw_prefix_smc as raw


def frame(count=70):
    cs = [Candle(i*300, 100+i*3, 102+i*3, 99+i*3, 101+i*3, 1) for i in range(count)]
    return MarketDataFrame("BTC", "M5", "fixture", "fixture", "spot", "UTC", "fixture", "fixture",
                           "fixture", "test", "DATA_READY", count*300, cs[-1].timestamp,
                           cs[-1].timestamp, None, cs, "DATA_READY")


def ev(kind, ts, name=None, tf="M5", **evidence):
    e = raw.event("BTC", tf, kind, "BULLISH", ts, ts+raw.TF[tf], evidence,
                  {"file": "synthetic", "sha256": "fixture"}, 100000, {"name": name or kind})
    if name:
        e["event_id"] = name
    return e


def fixture(internal=False, zone="FVG"):
    l = ev("LIQUIDITY", 0, "L", price=100, type=("Internal" if internal else "External")+" Sell-side Liquidity")
    sw = ev("SWEPT", 300, "S", level_price=100, level_type=l["evidence"]["type"], sweep_price=98)
    re = ev("RECLAIMED", 600, "R", level_price=100, level_type=l["evidence"]["type"])
    d = ev("DISPLACEMENT", 900, "D", strength="VALID")
    m = ev("BOS" if internal else "MSS", 900, "M", price=105)
    z = ev(zone, 900, "Z", lower=101, upper=102, low=99, high=101)
    es = {e["event_id"]: e for e in (l, sw, re, d, m, z)}
    rs = [raw.make_relation(l, sw, "SWEEP_OF", {}, True),
          raw.make_relation(sw, re, "RECLAIM_AFTER", {}, True),
          raw.make_relation(re, d, "DISPLACEMENT_CANDIDATE", {"sweep_id": "S"}),
          raw.make_relation(d, m, "STRUCTURE_CONSEQUENCE_CANDIDATE", {"exact_reference_break": True}, True),
          raw.make_relation(d, z, "FVG_CREATED_BY" if zone == "FVG" else "OB_ASSOCIATED_WITH", {}, True)]
    raw.ambiguity(rs, es)
    return es, rs


class RawPrefixSMCTests(unittest.TestCase):
    def test_01_post_checkpoint_excluded(self):
        f = frame(); p, es, _ = raw.extract({"M5": f}, "BTC", 9000, {})
        self.assertTrue(all(e["available_at"] <= 9000 for e in es.values()))
        self.assertTrue(all(c.timestamp+300 <= 9000 for c in p.candles["M5"]))

    def test_02_legally_available_raw_included(self):
        _, es, _ = raw.extract({"M5": frame()}, "BTC", 21000, {})
        self.assertTrue(any(e["kind"] == "FVG" and e["timestamp"] == 600 for e in es.values()))

    def test_03_no_display_truncation(self):
        f = frame(); ds = rv.find_displacements(f, rv.analyze_structure(f))
        full = raw.RAW_FVG(f, ds)
        self.assertGreater(len(full), 16)
        self.assertEqual(full[-16:], rv.find_fvgs(f, ds))

    def test_04_same_tf_parent(self):
        es, _ = fixture()
        self.assertEqual(raw.candidate_reason(es["R"], es["D"], 2000, {"price_local": True}), "PASS")

    def test_05_lower_tf_child(self):
        es, _ = fixture(); es["R"]["timeframe"] = "M15"
        self.assertIn("M5", raw.SWEEP_TFS["M15"])
        self.assertEqual(raw.candidate_reason(es["R"], es["D"], 2000, {"price_local": True}), "PASS")

    def test_06_higher_tf_structure_bridge(self):
        self.assertIn("H1", raw.STRUCTURE_TFS["M5"])
        self.assertIn("H1", raw.STRUCTURE_TFS["M15"])
        self.assertNotIn("D1", raw.STRUCTURE_TFS["M5"])

    def test_07_direction_rejected(self):
        es, _ = fixture(); es["D"]["direction"] = "BEARISH"
        self.assertEqual(raw.candidate_reason(es["R"], es["D"], 2000, {"price_local": True}), "DIRECTION_MISMATCH")

    def test_08_context_break_rejected(self):
        es, _ = fixture()
        self.assertEqual(raw.candidate_reason(es["R"], es["D"], 2000,
                                             {"price_local": True, "context_break": ["LOSS"]}), "CONTEXT_BREAK")

    def test_09_price_unrelated_rejected(self):
        es, _ = fixture()
        self.assertEqual(raw.candidate_reason(es["R"], es["D"], 2000, {"price_local": False}), "PRICE_UNRELATED")

    def test_10_multiple_parent_ambiguity(self):
        es, rs = fixture(); other = ev("RECLAIMED", 600, "R2")
        es["R2"] = other
        rs.append(raw.make_relation(other, es["D"], "DISPLACEMENT_CANDIDATE", {"sweep_id": "S2"}))
        raw.ambiguity(rs, es)
        self.assertEqual(es["D"]["candidate_parent_count"], 2)
        self.assertTrue(all(r["ancestry_state"] == "AMBIGUOUS" for r in rs if r["type"] == "DISPLACEMENT_CANDIDATE"))

    def test_11_multiple_child_ambiguity(self):
        es, rs = fixture(); d2 = ev("DISPLACEMENT", 1200, "D2", strength="VALID")
        es["D2"] = d2
        rs.append(raw.make_relation(es["R"], d2, "DISPLACEMENT_CANDIDATE", {"sweep_id": "S"}))
        raw.ambiguity(rs, es)
        self.assertEqual(es["S"]["candidate_child_count"], 2)
        self.assertEqual(raw.chain_candidates(es, rs, {"events": es})[0]["state"], "AMBIGUOUS_CHAIN_CANDIDATE")

    def test_12_exact_provenance(self):
        es, rs = fixture()
        self.assertEqual(rs[0]["ancestry_state"], "EXACT")
        self.assertTrue(rs[0]["candidate_only"])

    def test_13_ambiguous_mss_not_promoted(self):
        es, rs = fixture(); rs[2]["ancestry_state"] = "AMBIGUOUS"
        self.assertEqual(raw.contextual_candidates(es, rs)[0]["state"], "AMBIGUOUS")
        self.assertEqual(es["M"]["kind"], "MSS")

    def test_14_displacement_fvg_descendant(self):
        f = frame(); cs = f.candles
        cs[30] = Candle(9000, 189, 206, 189, 205, 1)
        _, es, rs = raw.extract({"M5": f}, "BTC", 21000, {})
        found = [r for r in rs if r["type"] == "FVG_CREATED_BY"]
        self.assertTrue(found)
        for r in found:
            self.assertEqual(es[r["parent"]]["timestamp"], es[r["child"]]["timestamp"])
            self.assertEqual(r["proof"]["research_type"], "CHAIN_CANDIDATE_FVG")

    def test_15_ob_descendant_not_last_opposite_alone(self):
        f = frame()
        self.assertEqual(raw.RAW_OB(f, rv.analyze_structure(f), []), [])
        es, rs = fixture(zone="OB")
        self.assertEqual(rs[-1]["type"], "OB_ASSOCIATED_WITH")
        self.assertEqual(raw.chain_candidates(es, rs, {"events": es})[0]["state"], "UNAMBIGUOUS_CHAIN_CANDIDATE")

    def test_16_contextual_mss_candidate(self):
        es, rs = fixture(); m = raw.contextual_candidates(es, rs)[0]
        self.assertEqual(m["classification"], "CONTEXTUAL_MSS_CANDIDATE")
        self.assertEqual(m["state"], "UNAMBIGUOUS")

    def test_17_reversal_chain(self):
        es, rs = fixture(); c = raw.chain_candidates(es, rs, {"events": es})[0]
        self.assertEqual(c["chain_type"], "REVERSAL")
        self.assertEqual(c["state"], "UNAMBIGUOUS_CHAIN_CANDIDATE")

    def test_18_continuation_chain_no_external_required(self):
        es, rs = fixture(True); c = raw.chain_candidates(es, rs, {"events": es})[0]
        self.assertEqual(c["chain_type"], "CONTINUATION")
        self.assertEqual(c["state"], "UNAMBIGUOUS_CHAIN_CANDIDATE")

    def test_19_original_snapshot_immutable(self):
        es, rs = fixture(); origin = {"events": es}; before = copy.deepcopy(origin)
        raw.chain_candidates(es, rs, origin)
        self.assertEqual(origin, before)

    def test_20_production_isolation(self):
        f = frame(); before = copy.deepcopy(f); original = rv.find_fvgs
        raw.extract({"M5": f}, "BTC", 21000, {})
        self.assertEqual(f, before)
        self.assertIs(rv.find_fvgs, original)
        self.assertNotEqual(raw.RAW_FVG, original)

    def test_21_deterministic_ids(self):
        self.assertEqual(raw.uid("REL", {"b": 2, "a": 1}), raw.uid("REL", {"a": 1, "b": 2}))

    def test_22_fresh_process_determinism(self):
        command = "from tests.test_raw_prefix_smc import frame; from historical_research.raw_prefix_smc import extract; import json,hashlib; p,e,r=extract({'M5':frame()},'BTC',21000,{}); print(hashlib.sha256(json.dumps([e,r],sort_keys=True).encode()).hexdigest())"
        results = [subprocess.check_output([sys.executable, "-c", command], env=dict(os.environ, PYTHONHASHSEED=seed, PYTHONDONTWRITEBYTECODE="1")) for seed in ("11", "97")]
        self.assertEqual(results[0], results[1])

    def test_23_no_outcome_access(self):
        with patch("builtins.open", side_effect=AssertionError("no file reads in reconstruction")):
            raw.extract({"M5": frame()}, "BTC", 21000, {})
        self.assertNotIn("MFE", inspect.getsource(raw))
        self.assertNotIn("MAE", inspect.getsource(raw))

    def test_24_uncapped_adapter_fails_closed(self):
        with self.assertRaises(ValueError):
            raw.uncapped(rv.find_fvgs, "gaps", 99)

    def test_25_confirmation_candle_not_sweep(self):
        f = frame(); f.candles[25] = Candle(7500, 175, 250, 174, 176, 1)
        _, es, rs = raw.extract({"M5": f}, "BTC", 21000, {})
        for r in rs:
            if r["type"] == "SWEEP_OF":
                self.assertGreaterEqual(es[r["child"]]["timestamp"], es[r["parent"]]["available_at"])

    def test_26_reactions_of_same_sweep_not_multiple_parents(self):
        es, rs = fixture(); rejection = ev("REJECTION", 600, "RJ"); es["RJ"] = rejection
        rs.append(raw.make_relation(rejection, es["D"], "DISPLACEMENT_CANDIDATE", {"sweep_id": "S"}))
        raw.ambiguity(rs, es)
        self.assertEqual(es["D"]["candidate_parent_count"], 1)

    def test_27_future_relation_unavailable(self):
        with self.assertRaises(ValueError):
            raw.event("BTC", "M5", "MSS", "BULLISH", 900, 1200, {}, {}, 1199, {})

    def test_28_weak_candidate_rejected_not_dropped_from_inventory(self):
        es, _ = fixture(); es["D"]["evidence"]["strength"] = "WEAK"
        self.assertEqual(raw.candidate_reason(es["R"], es["D"], 2000, {"price_local": True}), "WEAK_DISPLACEMENT")

    def test_29_generic_mss_without_ancestry(self):
        es, rs = fixture()
        m = raw.contextual_candidates(es, [r for r in rs if r["type"] != "DISPLACEMENT_CANDIDATE"])[0]
        self.assertEqual(m["classification"], "GENERIC_MSS")

    def test_30_partial_without_zone(self):
        es, rs = fixture()
        c = raw.chain_candidates(es, rs[:-1], {"events": es})[0]
        self.assertEqual(c["state"], "PARTIAL_CHAIN")

    def test_31_inventory_visibility_does_not_change_identity(self):
        es, rs = fixture()
        a = raw.chain_candidates(es, rs, {"events": es})[0]
        b = raw.chain_candidates(es, rs, {"events": {}})[0]
        self.assertEqual(a["candidate_chain_id"], b["candidate_chain_id"])
        self.assertEqual(a["state"], b["state"])
        self.assertNotEqual(a["inventory_visibility"], b["inventory_visibility"])

    def test_32_index_temporal_bounds(self):
        es, _ = fixture(); ix = raw.Index(es)
        self.assertEqual(ix.between("BTC", "M5", "DISPLACEMENT", 901, 1500), [])
        self.assertEqual(len(ix.between("BTC", "M5", "DISPLACEMENT", 900, 900)), 1)
        self.assertEqual(ix.between("BTC", "M5", "DISPLACEMENT", 900, 900, direction="BEARISH"), [])
        self.assertEqual(len(ix.between("BTC", "M5", "DISPLACEMENT", 900, 900, direction="BULLISH")), 1)

    def test_33_higher_tf_structure_requires_exact_price_consequence(self):
        es, rs = fixture()
        es["M"].update(timeframe="H1", timestamp=3600, open_timestamp=3600, available_at=7200)
        es["D"]["confirmation_evidence"] = {"strength": "VALID", "fvg_created": True}
        candles = [Candle(0,100,101,99,100,1), Candle(300,100,101,98,100,1),
                   Candle(600,99,108,98,105,1), Candle(900,104,111,103,110,1)]
        pre = SimpleNamespace(last_swing_high=rv.Swing("HIGH",105,0), last_swing_low=rv.Swing("LOW",90,0),
                              protected_high=None, protected_low=90, state="BULLISH")
        prefix = SimpleNamespace(candles={"M5":candles}, by_open={"M5":{c.timestamp:c for c in candles}},
                                 at=lambda tf, at: (None,pre,[]))
        initial = [r for r in rs if r["type"] not in {"DISPLACEMENT_CANDIDATE","STRUCTURE_CONSEQUENCE_CANDIDATE"}]
        origin = {"events":es, "episode_id":"fixture", "origin_checkpoint":100000}
        with patch.object(raw,"extract",return_value=(prefix,copy.deepcopy(es),copy.deepcopy(initial))):
            out = raw.reconstruct(origin,{})
        bridged = [r for r in out["relations"] if r["type"]=="STRUCTURE_CONSEQUENCE_CANDIDATE"]
        self.assertEqual(len(bridged),1)
        self.assertEqual(bridged[0]["proof"]["tf_relation"],"HIGHER_TF")
        self.assertEqual(bridged[0]["proof"]["reference_known_at"],900)
        es["M"]["evidence"]["price"]=200
        with patch.object(raw,"extract",return_value=(prefix,copy.deepcopy(es),copy.deepcopy(initial))):
            out = raw.reconstruct(origin,{})
        self.assertFalse(any(r["type"]=="STRUCTURE_CONSEQUENCE_CANDIDATE" for r in out["relations"]))

    def test_34_higher_tf_displacement_diagnostic_not_accepted(self):
        es, _ = fixture(); es["D"]["timeframe"]="H1"
        self.assertEqual(raw.candidate_reason(es["R"],es["D"],2000,{"price_local":True}),"TIMEFRAME_INCOMPATIBLE")

    def test_35_ob_uncapped_tail_equivalence(self):
        import random
        rng=random.Random(7); f=frame(400); price=1000; candles=[]
        for i in range(400):
            close=price+rng.uniform(-30,30)
            candles.append(Candle(i*300,price,max(price,close)+rng.uniform(.1,2),min(price,close)-rng.uniform(.1,2),close,1))
            price=close
        f.candles=candles
        st=rv.analyze_structure(f); ds=rv.find_displacements(f,st)
        full=raw.RAW_OB(f,st,ds)
        self.assertGreater(len(full),8)
        self.assertEqual(full[-8:],rv.find_order_blocks(f,st,ds))

    def test_36_equal_level_uncapped_tail_equivalence(self):
        f=frame(); swings=[rv.Swing("HIGH",100,i*900) for i in range(10)]
        full=raw.RAW_EQUAL(f,swings)
        self.assertGreater(len(full),3)
        self.assertEqual(full[-3:],rv._equal_levels(f,swings))

    def test_37_late_reclaim_retained_but_outside_ancestry_window(self):
        f=frame(20)
        f.candles=[Candle(i*300,105,106,104,105,1) for i in range(20)]
        f.candles[2]=Candle(600,105,106,100,105,1)
        f.candles[5]=Candle(1500,105,106,99,99,1)
        for i in range(6,15):
            f.candles[i]=Candle(i*300,99,100,98,99,1)
        f.candles[15]=Candle(4500,99,106,98,105,1)
        swing=rv.Swing("LOW",100,600)
        st=SimpleNamespace(swings=[swing],events=[],last_swing_high=None,last_swing_low=swing)
        def prefix_at(p,tf,at):
            return raw.visible(p.frames[tf],at),st,[]
        with patch.object(raw.Prefix,"at",prefix_at):
            _,es,rs=raw.extract({"M5":f},"BTC",6000,{})
        reclaim=next(e for e in es.values() if e["kind"]=="RECLAIMED")
        sweep=next(e for e in es.values() if e["kind"]=="SWEPT")
        level=next(e for e in es.values() if e["kind"]=="LIQUIDITY")
        self.assertEqual(reclaim["timestamp"],4500)
        self.assertGreater(reclaim["available_at"],sweep["available_at"]+5*300)
        self.assertEqual(level["status"],"RECLAIMED")
        self.assertTrue(any(r["type"]=="RECLAIM_AFTER" for r in rs))


if __name__ == "__main__":
    unittest.main()
