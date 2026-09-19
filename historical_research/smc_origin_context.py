"""Frozen decision-context extractor, preserved from smc-quality-v1/analyze.py."""
import copy
import hashlib
import json
import re

TF = {"D1": 86400, "H4": 14400, "H1": 3600, "M15": 900, "M5": 300}

def family(state, data, available=True, limitation=None):
    return {"state": state, "availability": "PARTIAL" if available else "UNAVAILABLE",
            "evidence": data, "limitation": limitation}
def checked_records(records, cutoff, checkpoint, timestamp="formed_at"):
    result = []
    for raw in records:
        v = copy.deepcopy(raw)
        tf, ts = v["timeframe"], v[timestamp]
        assert ts <= cutoff[tf] and ts + TF[tf] <= checkpoint, (tf, ts, checkpoint)
        v["research_evidence_id"] = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()[:20]
        v["known_at_checkpoint"] = checkpoint
        # formed_at is not necessarily OB confirmation time; do not relabel it.
        result.append(v)
    return result
def extract(o, direction):
    """Feature-only interface: no episode outcomes, lifecycle result or cohort input."""
    o = copy.deepcopy(o)
    r, fs, t = o["review"], o["decision_feature_snapshot"], o["checkpoint"]
    raw, truth = fs["raw_metrics"], fs["truth"]
    for tf, ts in o["data_cutoff"].items():
        assert ts + TF[tf] <= t
    assert o["available_at"] <= t
    for f in fs["features"].values():
        assert f["available_at"] <= t
    bias = "BULLISH" if direction == "LONG" else "BEARISH"
    levels = checked_records(r["Liquidity"], o["data_cutoff"], t)
    events = checked_records(r["Liquidity_Events"], o["data_cutoff"], t, "timestamp")
    gaps = checked_records(r["FVG"], o["data_cutoff"], t)
    blocks = checked_records(r["Order_Blocks"], o["data_cutoff"], t)
    for kind, zones in [("FVG", gaps), ("OB", blocks)]:
        for z in zones:
            z["htf_aligned"] = z["direction"] == r["Swing_Bias"]
            z["origin_direction_aligned"] = z["direction"] == bias
            z["created_by_displacement"] = bool(z.get("related_displacement_id")) if kind == "FVG" else True
            z["related_displacement_id"] = z.get("related_displacement_id")
            z["confirmation_timestamp"] = None
            z["premium_discount_location"] = None
            z["distance_to_target"] = None
            z["defense_success"] = None
            z["defense_failed_by_origin"] = z["status"] == "INVALIDATED"
    aligned_gaps = [z for z in gaps if z["origin_direction_aligned"]]
    aligned_blocks = [z for z in blocks if z["origin_direction_aligned"]]
    def zone_state(zones):
        if not zones:
            return "NO_ALIGNED_ZONE_IN_REPORTED_SET"
        if any(z["status"] == "FRESH" for z in zones):
            return "ALIGNED_FRESH_PRESENT"
        if any(z["status"] in {"TOUCHED", "PARTIALLY_MITIGATED"} for z in zones):
            return "ALIGNED_TESTED_PRESENT"
        return "ALIGNED_ONLY_MITIGATED_OR_INVALIDATED"

    intended = r["Macro_Draw_on_Liquidity"]
    match = re.fullmatch(r"Macro Draw: (.+?) ([0-9.]+) on (D1|H4|H1|M15|M5), distance=([0-9.]+); (.+)", intended)
    draw = None
    if match:
        typ, price, tf, distance, reason = match.groups()
        draw = {"draw_type": typ, "price": float(price), "draw_timeframe": tf,
                "draw_direction": r["Swing_Bias"], "distance_to_draw_fraction": float(distance),
                "selection_reason": reason}
    # Latest direction-relevant reported sweep, not a production active sweep.
    side = ("Sell-side", "Equal Lows") if direction == "LONG" else ("Buy-side", "Equal Highs")
    sweeps = [e for e in events if e["event_type"] == "SWEPT" and any(s in e["level_type"] for s in side)]
    sweep = max(sweeps, key=lambda e: (e["timestamp"] + TF[e["timeframe"]], e["timeframe"])) if sweeps else None
    reaction, scope, identity = "NO_CLEAR_REACTION", "NO_SWEEP_IN_REPORTED_SET", []
    aftermath = []
    if sweep:
        scope = "EXTERNAL_SWEEP" if "External" in sweep["level_type"] else "INTERNAL_SWEEP" if "Internal" in sweep["level_type"] else "EQUAL_LEVEL_SWEEP_SCOPE_UNRESOLVED"
        identity = [l["liquidity_id"] for l in levels if l["timeframe"] == sweep["timeframe"] and l["price"] == sweep["level_price"] and l["type"] == sweep["level_type"]]
        if len(set(identity)) == 1:
            aftermath = [e for e in events if e["timeframe"] == sweep["timeframe"] and e["level_price"] == sweep["level_price"] and e["level_type"] == sweep["level_type"] and e["timestamp"] > sweep["timestamp"]]
            if aftermath:
                last = max(aftermath, key=lambda e: e["timestamp"])
                reaction = {"RECLAIMED":"RECLAIMED", "FAILED_RECLAIM":"ACCEPTED_OUTSIDE"}.get(last["event_type"], "NO_CLEAR_REACTION")
    reclaim = next((e for e in sorted(aftermath,key=lambda e:e["timestamp"]) if e["event_type"] == "RECLAIMED"), None)
    displacement = {"reported": r["Displacement"], "timeframe": None, "immediate_overlap": None}
    dm = re.fullmatch(r"(BULLISH|BEARISH) (VALID|STRONG|WEAK) @ (\d+); structure_broken=(.*?); fvg_created=(True|False); body_ratio=([0-9.]+); range_ratio=([0-9.]+); close_near_extreme=(True|False); follow_through=(True|False)",r["Displacement"])
    if dm:
        d, strength, timestamp, broken, fvg, body, rng, close, follow = dm.groups()
        displacement.update(direction=d,strength=strength,timestamp=int(timestamp),structure_penetration=broken, fvg_created=fvg=="True", body_expansion=float(body),range_expansion=float(rng),close_near_extreme=close=="True",follow_through=follow=="True")
        assert int(timestamp) < t
    structure = []
    for tf, strings in r["Last_Structure_Events"].items():
        for text in strings:
            m = re.match(r"(MSS|BOS) (BULLISH|BEARISH) @ (\d+) price ([0-9.]+)", text)
            if m:
                kind,d,ts,price=m.groups()
                assert int(ts) <= o["data_cutoff"][tf] and int(ts)+TF[tf]<=t
                structure.append({"kind":kind,"direction":d,"timestamp":int(ts),"available_at":int(ts)+TF[tf],"timeframe":tf,"price":float(price),"evidence_id":tf+":"+text})
    relevant = [e for e in structure if e["direction"] == bias]
    after = [e for e in relevant if sweep and e["available_at"] > sweep["timestamp"]+TF[sweep["timeframe"]]]
    phase = r["Current_Phase"]
    pd = r["Premium_Discount"]
    favorable = (direction == "LONG" and pd == "DISCOUNT") or (direction == "SHORT" and pd == "PREMIUM")
    pdstate = "DIRECTION_COMPATIBLE_" + pd if favorable else "DIRECTION_EXTENDED_" + pd if pd in {"PREMIUM","DISCOUNT"} else pd
    protected = r["Protected_Low"] if direction == "LONG" else r["Protected_High"]
    # A source level is not proof that it remains on the protective side of price.
    seqstate = "PARTIAL_SEQUENCE" if dm or sweep else "NO_CLEAR_SEQUENCE"
    combinations = {}
    for name in ["SWEEP_DISPLACEMENT","SWEEP_DISPLACEMENT_MSS","SWEEP_DISPLACEMENT_MSS_FVG","SWEEP_RECLAIM_DISPLACEMENT_MSS_FVG_OR_OB"]:
        combinations[name] = "NOT_ESTABLISHED" if not sweep or not dm else "LINKAGE_UNVERIFIED"
    return {
        "checkpoint":t,"data_cutoff":o["data_cutoff"],"direction":direction,"phase":phase,"symbol":o["symbol"],
        "families":{
          "HTF_DRAW_ON_LIQUIDITY":family(draw["draw_type"] if draw else "NO_MACRO_DRAW",{"selected_draw":draw,"reported":intended,"reported_liquidity":levels,"opposing_liquidity_distance":None},True,"PDH/PDL/PWH/PWL and opposing-target distance not exposed; no new target selection."),
          "SWEEP_QUALITY":family(scope+"/"+reaction,{"selected_context_sweep":sweep,"level_identity_candidates":identity,"aftermath":aftermath,"sweep_depth_price":sweep["penetration"] if sweep else None,"reclaim_latency_seconds":reclaim["timestamp"]-sweep["timestamp"] if reclaim else None,"closed_back_inside":True if reclaim else None,"displacement_followed":None,"all_reported_events":events},True,"Bounded reported context, not active-sequence evidence. No events means no reported sweep, not universal NO_SWEEP. FAILED_RECLAIM mapped to accepted-outside only after sweep."),
          "DISPLACEMENT_QUALITY":family(displacement.get("strength","NOT_REPORTED"),displacement,bool(dm),"Expansion is previous-20 median body/range ratio, not ATR or percent. TF absent in report; no causal sweep link. Follow-through only known by origin."),
          "MSS_BOS_CONTEXT":family("MSS_AND_BOS" if {e["kind"] for e in relevant}=={"MSS","BOS"} else next(iter({e["kind"] for e in relevant}),"NONE_REPORTED"),{"events":structure,"aligned_events":relevant,"events_available_after_context_sweep":after,"causal_sequence_link":None,"contextual_mss":r["Contextual_MSS"],"location_relative_to_dealing_range":None},True,"Latest four breaks per timeframe only. Temporal co-occurrence is not linkage."),
          "FVG_QUALITY":family(zone_state(aligned_gaps),{"zones":gaps,"aligned_count":len(aligned_gaps),"active_setup":truth["setup_poi"]},True,"Reported last16/TF. Source setup_type means displacement-linked generic inventory; NOT active setup. Fill fraction and zone PD mapping unavailable."),
          "OB_QUALITY":family(zone_state(aligned_blocks),{"zones":blocks,"aligned_count":len(aligned_blocks)},True,"Existing OB requires valid displacement plus break and prior opposite candle. Repricing identity/confirmation time omitted. No prospective defense success claim."),
          "PREMIUM_DISCOUNT_LOCATION":family(pdstate,{"location":pd,"direction":direction,"normalized_location":None,"range_edge_distance_pct":raw["LOCATION"]["range_edge_distance_pct"],"dealing_range_bounds":None},True,"Existing legal range classification; bounds absent from archived review. Compatibility not quality verdict."),
          "REMAINING_ROOM_CONTEXT":family("NUMERIC_ROOM_UNAVAILABLE",{"remaining_room_pct":raw["OPPORTUNITY"]["remaining_room_pct"],"remaining_room_r_if_defined":raw["OPPORTUNITY"]["remaining_room_r_if_defined"],"selected_draw":draw,"opposing_distance":None,"trend_maturity":raw["CONTEXT"]["trend_maturity"],"extension_from_genesis_atr":raw["OPPORTUNITY"]["extension_from_genesis_atr"],"risk_signatures":fs["risk_signatures"]},True,"Extension and draw context available; sufficient-room classification unavailable. No invented formula."),
          "DEFENSE_QUALITY":family("DEFENSE_STRENGTH_NOT_EVALUATED",{"protected_levels":protected,"fresh_aligned_fvg_present":any(z["status"]=="FRESH" for z in aligned_gaps),"fresh_aligned_ob_present":any(z["status"]=="FRESH" for z in aligned_blocks),"reclaim_level":reclaim["level_price"] if reclaim else None,"displacement_origin":None,"candidate_states":["STRONG_DEFENSE","MODERATE_DEFENSE","WEAK_DEFENSE","NO_CLEAR_DEFENSE"]},True,"Components available; no validated strength rubric or current defendability/linkage proof. Do not assign strength by component count."),
          "SMC_SEQUENCE_COMPLETENESS":family(seqstate,{"presence":{"reported_sweep":bool(sweep),"reclaim":bool(reclaim),"displacement":bool(dm),"mss":any(e["kind"]=="MSS" for e in relevant),"fvg":bool(aligned_gaps),"ob":bool(aligned_blocks),"eligible_retest":truth["eligible_retest"]},"ordered_combinations":combinations,"phase_context":phase,"production_sequence_state":truth["sequence_state"],"full_sequence":False},True,"Partial reported presence, not coherent chain. FULL cannot be established without linked event identities/confirmation timestamps.")
        },
        "smt":{"state":"UNAVAILABLE","evaluation":"NOT_EVALUATED","reason":"No paired confirmed SMT evidence in origin archive; no new SMT detector introduced."}
    }
