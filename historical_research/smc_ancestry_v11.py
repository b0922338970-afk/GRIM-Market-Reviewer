"""Outcome-blind ancestry research. Never writes live state or calls production review."""
import copy
import hashlib
import json
from dataclasses import asdict, replace
from market_reviewer import reviewer as detector
from market_reviewer.model import TIMEFRAME_SECONDS as TF

VERSION = "smc-provenance.research.v1.1"
WINDOW_PIVOT_SPAN = 5  # Existing two-left / centre / two-right swing footprint.


def identity(kind, *parts):
    raw = json.dumps([VERSION, kind, *parts], sort_keys=True, separators=(",", ":"))
    return kind + "-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def visible(frame, checkpoint):
    cs = [c for c in frame.closed_candles() if c.timestamp + TF[frame.timeframe] <= checkpoint]
    if not cs:
        return None
    return replace(frame, candles=cs, latest_closed_candle_timestamp=cs[-1].timestamp,
                   latest_candle_timestamp=cs[-1].timestamp, current_open_candle_timestamp=None,
                   fetch_timestamp=checkpoint)


def deadline(sweep):
    return sweep["available_at"] + WINDOW_PIVOT_SPAN * TF[sweep["timeframe"]]


def relation(parent, child, kind, reason, source):
    if parent["symbol"] != child["symbol"] or parent["direction"] != child["direction"]:
        raise ValueError("symbol/direction mismatch")
    return {
        "relationship_id": identity("REL", kind, parent["event_id"], child["event_id"]),
        "parent_event_id": parent["event_id"], "child_event_id": child["event_id"],
        "parent": parent["event_id"], "child": child["event_id"], "type": kind,
        "relationship_type": kind, "relationship_reason": reason,
        "time_delta": child["timestamp"] - parent["timestamp"],
        "availability_delta": child["available_at"] - parent["available_at"],
        "direction_compatibility": True,
        "timeframe_compatibility": TF[child["timeframe"]] <= TF[parent["timeframe"]],
        "available_at": max(parent["available_at"], child["available_at"]),
        "source_evidence": source,
    }


def validate_sweep(level, sweep):
    e = sweep["evidence"]
    return (level["symbol"] == sweep["symbol"] and level["direction"] == sweep["direction"]
            and level["timeframe"] == sweep["timeframe"]
            and level["evidence"]["price"] == e["level_price"]
            and level["evidence"]["type"] == e["level_type"]
            and sweep["timestamp"] >= level["available_at"] and e["penetration"] > 0)


def validate_reaction(sweep, reaction):
    return (sweep["symbol"] == reaction["symbol"] and sweep["direction"] == reaction["direction"]
            and sweep["timeframe"] == reaction["timeframe"]
            and reaction["timestamp"] >= sweep["available_at"]
            and reaction["available_at"] <= deadline(sweep)
            and reaction["evidence"]["level_price"] == sweep["evidence"]["level_price"]
            and reaction["evidence"]["close_location"] == "INSIDE")


def sweep_state(sweep, reactions, candles, checkpoint):
    rs = [r for r in reactions if validate_reaction(sweep, r) and r["available_at"] <= checkpoint]
    if rs:
        first = min(rs, key=lambda r: (r["available_at"], r["kind"] != "RECLAIMED", r["event_id"]))
        return ("SWEEP_REJECTED" if first["kind"] == "REJECTION" else "SWEEP_RECLAIMED"), first["available_at"]
    end = deadline(sweep)
    c = next((c for c in candles if c.timestamp + TF[sweep["timeframe"]] == end), None)
    if end <= checkpoint and c:
        p = sweep["evidence"]["level_price"]
        if c.close < p if sweep["direction"] == "BULLISH" else c.close > p:
            return "SWEEP_ACCEPTED_OUTSIDE", end
    return "SWEEP_UNRESOLVED", None


def qualify(sweep, displacement, reaction, proof):
    if sweep["symbol"] != displacement["symbol"] or sweep["direction"] != displacement["direction"]:
        return "SYMBOL_DIRECTION_MISMATCH"
    if TF[displacement["timeframe"]] > TF[sweep["timeframe"]]:
        return "TIMEFRAME_INCOMPATIBLE"
    if reaction and not validate_reaction(sweep, reaction):
        return "WRONG_REACTION"
    if displacement["timestamp"] < (reaction or sweep)["available_at"]:
        return "TEMPORAL_ORDER"
    if displacement["available_at"] > deadline(sweep):
        return "CAUSAL_WINDOW_EXPIRED"
    if proof.get("context_break"):
        return "CONTEXT_BREAK"
    for field, reason in [
        ("level_price_interaction", "UNRELATED_PRICE_ACTION"),
        ("reference_known_before_sweep", "REFERENCE_UNAVAILABLE"),
        ("meaningful_structure_penetration", "NO_STRUCTURE_PENETRATION"),
        ("generic_break_matches_reference", "STRUCTURE_REFERENCE_MISMATCH"),
    ]:
        if not proof.get(field):
            return reason
    return "PASS"


def contextual_kind(level, generic_kind, reaction, phase):
    scope = level["evidence"]["type"]
    if reaction and generic_kind == "MSS":
        return "CONTEXTUAL_MSS"
    if "Internal" in scope and generic_kind == "BOS" and phase in {"CONTINUATION", "PULLBACK"}:
        return "CONTEXTUAL_BOS_CONTINUATION"
    return None


def build_chains(events, relations, checkpoint, prior_breaks):
    if any(e["available_at"] > checkpoint for e in events.values()):
        raise ValueError("future chain event")
    if any(r["available_at"] > checkpoint for r in relations.values()):
        raise ValueError("future relationship")
    adj = {k: set() for k in events}; parents = {k: set() for k in events}
    for r in relations.values():
        a, b = r["parent"], r["child"]
        adj[a].add(b); adj[b].add(a); parents[b].add(a)
    unseen = set(events); output = []
    while unseen:
        stack = [min(unseen)]; component = set()
        while stack:
            k = stack.pop()
            if k not in component:
                component.add(k); stack.extend(adj[k] - component)
        unseen -= component
        roots = sorted(k for k in component if not parents[k])
        es = [events[k] for k in sorted(component)]
        known = min(events[k]["available_at"] for k in roots)
        breaks = [b for b in prior_breaks if b["anchor_id"] in component and known <= b["available_at"] <= checkpoint]
        sweeps = [e for e in es if e["kind"] == "SWEPT"]
        ds = [e for e in es if e["kind"] == "DISPLACEMENT"]
        reactions = [e for e in es if e["kind"] in {"RECLAIMED", "REJECTION"}]
        for sw in sweeps:
            end = deadline(sw)
            reason = None
            if sw["evidence"].get("aftermath") == "SWEEP_ACCEPTED_OUTSIDE":
                reason = "SWEEP_ACCEPTED_OUTSIDE"
            elif (checkpoint >= end and sw["evidence"].get("causal_window_covered") and not ds
                  and not sw["evidence"].get("displacement_observed_in_window")):
                reason = "RECLAIM_NO_DISPLACEMENT" if reactions else "SWEEP_NO_RECLAIM"
            if reason:
                breaks.append({"anchor_id":sw["event_id"], "event_id":sw["event_id"],
                               "available_at":end, "reason":reason})
        first = min((b["available_at"] for b in breaks), default=None)
        breaks = sorted([b for b in breaks if b["available_at"] == first],
                        key=lambda b:(b["reason"], b["event_id"]))
        included = {k for k in component if first is None or events[k]["available_at"] <= first}
        es = [events[k] for k in sorted(included)]; kinds = {e["kind"] for e in es}
        def has_link(names):
            return any(r["type"] in names and r["child"] in included and r["parent"] in included
                       for r in relations.values())
        ld = has_link({"DISPLACEMENT_AFTER_SWEEP"})
        lr = has_link({"DISPLACEMENT_AFTER_RECLAIM"})
        cm = "CONTEXTUAL_MSS" in kinds; cb = "CONTEXTUAL_BOS_CONTINUATION" in kinds
        zone = has_link({"FVG_CREATED_BY", "OB_ASSOCIATED_WITH"})
        zone_parents = {r["parent"] for r in relations.values()
                        if r["type"] in {"FVG_CREATED_BY", "OB_ASSOCIATED_WITH"}
                        and r["parent"] in included and r["child"] in included}
        def contextual_zone(kind):
            return any(r["parent"] in zone_parents and r["child"] in included
                       and events[r["child"]]["kind"] == kind for r in relations.values())
        cm_zone = contextual_zone("CONTEXTUAL_MSS")
        cb_zone = contextual_zone("CONTEXTUAL_BOS_CONTINUATION")
        depth = "L5" if cm_zone else "L4" if cm else "L3" if lr else "L2" if ld else "L1" if zone else "L0"
        external = any(e["kind"] == "LIQUIDITY" and "External" in e["evidence"]["type"] for e in es)
        typ = "FAILED_CHAIN" if breaks else "REVERSAL_CHAIN" if cm_zone and external else "CONTINUATION_CHAIN" if cb_zone else "PARTIAL_CHAIN"
        sub = ("MSS_NO_LINKED_IMBALANCE" if cm and not cm_zone else
               "DISPLACEMENT_STRUCTURE_NO_FVG" if "DISPLACEMENT" in kinds and ({"MSS","BOS"} & kinds) and not zone else
               "SWEEP_DISPLACEMENT_NO_MSS" if ld and not cm else
               "SWEEP_RECLAIM_ONLY" if "SWEPT" in kinds and reactions and not ld else
               "SWEEP_ONLY" if "SWEPT" in kinds and not ld else "OTHER_PARTIAL")
        output.append({"smc_chain_id":identity("CHAIN",roots), "chain_type":typ, "partial_state":sub,
                       "ancestry_depth":depth, "direction":es[0]["direction"], "root_event_ids":roots,
                       "event_ids":sorted(included), "excluded_post_break_event_ids":sorted(component-included),
                       "chain_status":"BROKEN" if breaks else "OPEN",
                       "origin_timestamp":min(e["timestamp"] for e in es),
                       "latest_timestamp":max(e["timestamp"] for e in es),
                       "context_breaks":breaks, "failure_states":sorted({b["reason"] for b in breaks}),
                       "break_available_at":first, "has_contextual_continuation_bos":cb})
    return sorted(output, key=lambda c:c["smc_chain_id"])


def reconstruct(v1, frames):
    checkpoint = v1["origin_checkpoint"]
    events = copy.deepcopy(v1["events"]); relations = {}; rejected = []
    for e in events.values():
        if e["available_at"] > checkpoint or e["timestamp"] + TF[e["timeframe"]] > e["available_at"]:
            raise ValueError("future event")
        if e["kind"] == "LIQUIDITY":
            e["evidence"].update(side="SELL_SIDE" if e["direction"] == "BULLISH" else "BUY_SIDE",
                                 origin_timestamp=e["timestamp"])
    for r in v1["relations"].values():
        if r["type"] in {"FVG_CREATED_BY","OB_ASSOCIATED_WITH","STRUCTURE_PENETRATED_BY","RETEST_OF","DEFENSE_OF"}:
            rr = relation(events[r["parent"]], events[r["child"]], r["type"], r["proof"], [r["relationship_id"]])
            relations[rr["relationship_id"]] = rr
    sweeps = []; by_sweep = {}; level_of = {}
    for r in v1["relations"].values():
        if r["type"] != "SWEEP_OF":
            continue
        level, sw = events[r["parent"]], events[r["child"]]
        if not validate_sweep(level,sw):
            continue
        level["evidence"].update(side="SELL_SIDE" if level["direction"]=="BULLISH" else "BUY_SIDE",
                                 origin_timestamp=level["timestamp"])
        rr=relation(level,sw,"SWEEP_OF","exact liquidity identity + existing penetration semantics",[r["relationship_id"]])
        relations[rr["relationship_id"]]=rr; sweeps.append(sw)
        level_of[sw["event_id"]]=level; by_sweep[sw["event_id"]]=[]
    for r in v1["relations"].values():
        if r["type"] != "RECLAIM_AFTER" or r["parent"] not in by_sweep:
            continue
        sw, rec = events[r["parent"]], events[r["child"]]
        if validate_reaction(sw,rec):
            rr=relation(sw,rec,"RECLAIM_AFTER","same swept level and later inside close",[r["relationship_id"]])
            relations[rr["relationship_id"]]=rr; by_sweep[sw["event_id"]].append(rec)
            rec["evidence"].update(latency_seconds=rec["timestamp"]-sw["timestamp"],
                                  latency_candles=(rec["timestamp"]-sw["timestamp"])//TF[sw["timeframe"]])
    for sw in sweeps:
        cs=frames[sw["timeframe"]].closed_candles(); price=sw["evidence"]["level_price"]
        bull=sw["direction"]=="BULLISH"
        for c in cs:
            if c.timestamp < sw["available_at"] or c.timestamp+TF[sw["timeframe"]]>min(deadline(sw),checkpoint):
                continue
            rejects=(c.low<price<c.close and c.close>c.open) if bull else (c.high>price>c.close and c.close<c.open)
            if rejects:
                key=identity("REJECTION",sw["event_id"],c.timestamp)
                rec=dict(sw,event_id=key,kind="REJECTION",timestamp=c.timestamp,
                         available_at=c.timestamp+TF[sw["timeframe"]],rejection_event_id=key,
                         method="research rejection: exact swept-price re-probe and directional close inside",
                         evidence=dict(asdict(c),level_price=price,close_location="INSIDE",
                                       latency_candles=(c.timestamp-sw["timestamp"])//TF[sw["timeframe"]]))
                rec.pop("sweep_event_id",None)
                events[key]=rec; by_sweep[sw["event_id"]].append(rec)
                rr=relation(sw,rec,"REJECTION_AFTER","post-sweep re-probe beyond price; directional body closes inside",
                            [sw["event_id"],asdict(c)])
                relations[rr["relationship_id"]]=rr
                break
        state, at=sweep_state(sw,by_sweep[sw["event_id"]],cs,checkpoint)
        closed_opens = {c.timestamp for c in cs if c.timestamp + TF[sw["timeframe"]] <= checkpoint}
        covered = all(t in closed_opens for t in range(sw["available_at"], deadline(sw), TF[sw["timeframe"]]))
        if not covered and state == "SWEEP_ACCEPTED_OUTSIDE":
            state, at = "SWEEP_UNRESOLVED", None
        sw["evidence"].update(aftermath=state,aftermath_available_at=at,causal_deadline=deadline(sw),
                              causal_window_covered=covered)
    ds=[e for e in events.values() if e["kind"]=="DISPLACEMENT"]; pre_cache={}; d_cache={}
    for sw in sweeps:
        sw["evidence"]["displacement_observed_in_window"] = any(
            d["direction"] == sw["direction"] and TF[d["timeframe"]] <= TF[sw["timeframe"]]
            and d["timestamp"] >= sw["available_at"] and d["available_at"] <= deadline(sw) for d in ds)
    for d in ds:
        candidates=[]; d["evidence"]["parentage"]="DISPLACEMENT_UNLINKED"
        for sw in sweeps:
            if d["direction"]!=sw["direction"] or TF[d["timeframe"]]>TF[sw["timeframe"]]:
                continue
            if d["timestamp"]<sw["available_at"] or d["available_at"]>deadline(sw):
                continue
            eligible=[r for r in by_sweep[sw["event_id"]] if r["available_at"]<=d["timestamp"]]
            rec=min(eligible,key=lambda r:(r["available_at"],r["kind"]!="RECLAIMED",r["event_id"])) if eligible else None
            fr=frames[d["timeframe"]]; pk=(d["timeframe"],sw["timestamp"])
            if pk not in pre_cache:
                pf=visible(fr,sw["timestamp"])
                pre_cache[pk]=detector.analyze_structure(pf) if pf else None
            pre=pre_cache[pk]
            if d["event_id"] not in d_cache:
                df=visible(fr,d["available_at"]); st=detector.analyze_structure(df)
                d_cache[d["event_id"]]=(df,st,detector.find_displacements(df,st))
            df,st,other_d=d_cache[d["event_id"]]
            candle=next(c for c in df.closed_candles() if c.timestamp==d["timestamp"])
            bull=d["direction"]=="BULLISH"; price=sw["evidence"]["level_price"]
            ref=(pre.last_swing_high if bull else pre.last_swing_low) if pre else None
            generic=next((e for e in st.events if e.timestamp==d["timestamp"] and e.direction==d["direction"]),None)
            cross=(candle.open<=price<candle.close) if bull else (candle.open>=price>candle.close)
            rc=next((c for c in frames[rec["timeframe"]].closed_candles() if c.timestamp==rec["timestamp"]),None) if rec else None
            interacts=(candle.low<=rc.high and candle.high>=rc.low) if rc else cross
            broken=[]; start=sw["available_at"]
            for e in st.events:
                if start<=e.timestamp<d["timestamp"] and e.direction!=d["direction"]:
                    broken.append("OPPOSITE_STRUCTURE")
            for other in other_d:
                if start<=other.timestamp<d["timestamp"] and other.direction!=d["direction"] and other.strength in {"VALID","STRONG"}:
                    broken.append("OPPOSING_DISPLACEMENT")
            for c in df.closed_candles():
                if not start<=c.timestamp<d["timestamp"]:
                    continue
                if c.close<sw["evidence"]["sweep_price"] if bull else c.close>sw["evidence"]["sweep_price"]:
                    broken.append("SWEEP_EXTREME_INVALIDATED")
                if rec and c.timestamp >= rec["available_at"] and (c.close<price if bull else c.close>price):
                    broken.append("LIQUIDITY_THESIS_INVALIDATED")
                protected = (pre.protected_low if bull else pre.protected_high) if pre else None
                if pre and pre.state == d["direction"] and protected is not None:
                    if c.close < protected if bull else c.close > protected:
                        broken.append("PROTECTED_LEVEL_INVALIDATED")
            proof={"reference_known_before_sweep":ref is not None,"reference":asdict(ref) if ref else None,
                   "protected_reference":(pre.protected_high if bull else pre.protected_low) if pre else None,
                   "breaks_protected_structure":bool(pre and generic and generic.price == (pre.protected_high if bull else pre.protected_low)),
                   "breaks_confirmed_swing":bool(ref and generic and generic.price == ref.price),
                   "reference_checkpoint":sw["timestamp"],"level_price_interaction":interacts,
                   "meaningful_structure_penetration":bool(ref and (candle.close>ref.price if bull else candle.close<ref.price)),
                   "generic_break_matches_reference":bool(ref and generic and generic.price==ref.price),
                   "context_break":sorted(set(broken)),"displacement_candle":asdict(candle),
                   "generic_event":asdict(generic) if generic else None}
            reason=qualify(sw,d,rec,proof)
            if reason=="PASS":
                candidates.append((sw,rec,proof,generic))
            else:
                rejected.append({"displacement_event_id":d["event_id"],"sweep_event_id":sw["event_id"],
                                 "reason":reason,"proof":proof})
        if len(candidates)!=1:
            if len(candidates)>1:
                rejected.append({"displacement_event_id":d["event_id"],"reason":"AMBIGUOUS_LIQUIDITY_ANCESTRY",
                                 "candidate_sweeps":[x[0]["event_id"] for x in candidates]})
            continue
        sw,rec,proof,generic=candidates[0]
        for parent,typ in [(sw,"DISPLACEMENT_AFTER_SWEEP")]+([(rec,"DISPLACEMENT_AFTER_RECLAIM")] if rec else []):
            rr=relation(parent,d,typ,"bounded price interaction + pre-sweep confirmed swing penetration; no context break",proof)
            relations[rr["relationship_id"]]=rr
        d["evidence"].update(parentage="DISPLACEMENT_AFTER_RECLAIM" if rec else "DISPLACEMENT_AFTER_SWEEP",
            parent_sweep_id=sw["event_id"],parent_reaction_id=rec["event_id"] if rec else None,penetration_provenance=proof)
        sid = identity("STRUCTURE", d["symbol"], d["timeframe"], generic.direction,
                       generic.kind, generic.timestamp, generic.price)
        structure_event = dict(d, event_id=sid, kind=generic.kind,
                               structure_event_id=sid, timestamp=generic.timestamp,
                               available_at=d["available_at"],
                               method="generic break reverified at displacement confirmation prefix",
                               evidence=dict(asdict(generic), classification="GENERIC_" + generic.kind))
        structure_event.pop("displacement_event_id", None)
        events[sid] = structure_event
        d["evidence"]["structure_event_id"] = sid
        rr = relation(d, structure_event, "STRUCTURE_PENETRATION",
                      "exact break price matches pre-sweep confirmed reference; conservative prefix availability", proof)
        relations[rr["relationship_id"]] = rr
        at_d = {tf: visible(frame, d["available_at"]) for tf, frame in frames.items()}
        phase_at_d = None
        if all(at_d.values()):
            structures = {tf: detector.analyze_structure(frame) for tf, frame in at_d.items()}
            bias = detector._swing_bias(structures)
            phase_at_d = detector._current_phase(bias, structures, at_d["M5"].closed_candles()[-1].close)
        proof["phase_at_displacement_confirmation"] = phase_at_d
        kind=contextual_kind(level_of[sw["event_id"]],generic.kind,rec,phase_at_d)
        if kind:
            key=identity(kind,sw["event_id"],d["event_id"],d["timeframe"],generic.timestamp,generic.price)
            event=dict(d,event_id=key,kind=kind,timestamp=generic.timestamp,
                       available_at=max(d["available_at"],generic.timestamp+TF[d["timeframe"]]),
                       contextual_structure_event_id=key,evidence={
                       "generic_kind":generic.kind,"classification":kind,"generic_event":asdict(generic),
                       "liquidity_event_id":level_of[sw["event_id"]]["event_id"],"sweep_event_id":sw["event_id"],
                       "reaction_event_id":rec["event_id"] if rec else None,"displacement_event_id":d["event_id"],
                       "structure_event_id":sid})
            event.pop("displacement_event_id",None); events[key]=event
            rr=relation(d,event,kind+"_AFTER","same exact break of pre-sweep reference; context waits for D confirmation",proof)
            relations[rr["relationship_id"]]=rr
    prior=[]
    for c in v1["chains"]:
        for b in c["context_breaks"]:
            for root in c["root_event_ids"]:
                prior.append(dict(b,anchor_id=root))
    ch=build_chains(events,relations,checkpoint,prior)
    aligned=[c for c in ch if c["direction"]==("BULLISH" if v1["direction"]=="LONG" else "BEARISH") and len(c["event_ids"])>1]
    primary=max(aligned,key=lambda c:(min(events[k]["available_at"] for k in c["root_event_ids"]),c["smc_chain_id"])) if aligned else None
    return {"schema":VERSION,"episode_id":v1["episode_id"],"origin_checkpoint":checkpoint,"direction":v1["direction"],
            "phase":v1["phase"],"events":events,"relations":relations,"rejected_links":rejected,
            "chains":ch,"primary_chain":primary}
