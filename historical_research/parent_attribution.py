"""Outcome-blind, conservative attribution over frozen raw-prefix candidates."""
from collections import Counter, defaultdict
from bisect import bisect_left, bisect_right
from .raw_prefix_smc import TF, SWEEP_TFS, uid

VERSION = "smc-parent-attribution.v1"
SUPPORT = "SUPPORTS_PARENT"
NEUTRAL = "NEUTRAL"
CONTRADICT = "CONTRADICTS_PARENT"
UNAVAILABLE = "UNAVAILABLE"
FACTORS = ("TEMPORAL_PROXIMITY", "PRICE_LOCALITY", "DIRECTION_COMPATIBILITY", "LIQUIDITY_RELEVANCE",
           "RECLAIM_CONTINUITY", "STRUCTURE_CONSEQUENCE", "TIMEFRAME_COMPATIBILITY", "CONTEXT_CONTINUITY", "SUPERSESSION_STATE")


def factor(state, **evidence):
    return {"state": state, "evidence": evidence}


def liquidity_class(level):
    typ = level["evidence"].get("type", "")
    for text, name in (("PDH", "PDH/PDL"), ("PDL", "PDH/PDL"), ("PWH", "PWH/PWL"), ("PWL", "PWH/PWL"),
                       ("Equal", "EQH/EQL"), ("External", "EXTERNAL"), ("Internal", "INTERNAL")):
        if text in typ:
            return name
    return "HTF_SWING" if level["timeframe"] in {"D1", "H4"} else "OTHER"


class ClosedIndex:
    def __init__(self, frames, checkpoint):
        self.rows = {tf: [c for c in f.closed_candles() if c.timestamp + TF[tf] <= checkpoint]
                     for tf, f in frames.items()}
        self.opens = {tf: [c.timestamp for c in cs] for tf, cs in self.rows.items()}

    def between(self, tf, start, end):
        opens = self.opens.get(tf, [])
        # Fully closed BEFORE D open: no use of a parent-TF candle overlapping D.
        return self.rows.get(tf, [])[bisect_left(opens, start):bisect_right(opens, end-TF[tf])]


def structure_evidence(graph, displacement, sweep, relation):
    structs = [r for r in graph["relations"] if r["type"] == "STRUCTURE_CONSEQUENCE_CANDIDATE"
               and r["parent"] == displacement["event_id"] and r["available_at"] <= graph["origin_checkpoint"]]
    ref = relation["proof"].get("confirmed_reference")
    matching = [r for r in structs if ref and r["proof"].get("reference") == ref["price"]]
    # Optional explicit detector identity is a proof contract, never inferred from proximity.
    # The current raw-prefix schema supplies no such identity; real EXACT counts may be zero.
    explicit = [p for p in graph.get("explicit_parent_identities", [])
                if p.get("displacement_id") == displacement["event_id"]
                and p.get("proof_type") == "EXPLICIT_DETECTOR_REFERENCE"
                and p.get("source_event_id") in graph["events"]
                and p.get("sweep_id") in graph["events"]
                and graph["events"][p["sweep_id"]]["kind"] == "SWEPT"
                and graph["events"][p["sweep_id"]]["symbol"] == displacement["symbol"]
                and graph["events"][p["sweep_id"]]["direction"] == displacement["direction"]
                and graph["events"][p["sweep_id"]]["available_at"] <= displacement["timestamp"]
                and (p["source_event_id"] == displacement["event_id"] or any(r["child"] == p["source_event_id"] and r["available_at"] <= p.get("available_at", -1) for r in structs))
                and graph["events"][p["source_event_id"]]["available_at"] <= p.get("available_at", -1)
                <= graph["origin_checkpoint"]]
    exact_ids = {p["sweep_id"] for p in explicit}
    state = CONTRADICT if exact_ids and sweep["event_id"] not in exact_ids else SUPPORT if matching or exact_ids else NEUTRAL if structs else UNAVAILABLE
    return factor(state, generic_structure_ids=sorted({r["child"] for r in structs}),
                  matching_reference_ids=sorted(r["child"] for r in matching),
                  exact_break_references=sorted({r["proof"].get("reference") for r in structs if r["proof"].get("reference") is not None}),
                  explicit_sweep_ids=sorted(exact_ids),
                  confirmed_swing_penetration=relation["proof"].get("structure_penetrating"),
                  protected_structure_consequence=any(r["proof"].get("protected_structure_penetration") for r in matching),
                  note="Mismatching generic references alone do not contradict parent ancestry")


def evaluate_route(graph, relation, candles, by_sweep):
    es = graph["events"]; d = es[relation["child"]]; parent = es[relation["parent"]]
    sw = es[relation["proof"]["sweep_id"]]; level = es[relation["proof"]["liquidity_id"]]
    end = sw["available_at"] + 5 * TF[level["timeframe"]]
    proof = relation["proof"]
    f = {k: factor(UNAVAILABLE) for k in FACTORS}
    f["TEMPORAL_PROXIMITY"] = factor(SUPPORT if parent["available_at"] <= d["timestamp"] and d["available_at"] <= end else CONTRADICT,
        sweep_available_at=sw["available_at"], reclaim_available_at=parent["available_at"] if parent["kind"] != "SWEPT" else None,
        displacement_open_time=d["timestamp"], displacement_available_at=d["available_at"],
        time_delta=d["timestamp"]-parent["available_at"], deadline=end, nearest_preference=False)
    f["PRICE_LOCALITY"] = factor(SUPPORT if proof.get("price_local") is True else CONTRADICT if proof.get("price_local") is False else UNAVAILABLE,
                                reference_price=sw["evidence"].get("level_price"), source_relation_id=relation["relation_id"])
    f["DIRECTION_COMPATIBILITY"] = factor(SUPPORT if d["symbol"] == sw["symbol"] and d["direction"] == sw["direction"] else CONTRADICT)
    f["TIMEFRAME_COMPATIBILITY"] = factor(SUPPORT if sw["timeframe"] in SWEEP_TFS[level["timeframe"]]
                                         and d["timeframe"] in SWEEP_TFS[parent["timeframe"]] else CONTRADICT,
                                         liquidity_tf=level["timeframe"], sweep_tf=sw["timeframe"], displacement_tf=d["timeframe"])
    cls = liquidity_class(level)
    f["LIQUIDITY_RELEVANCE"] = factor(NEUTRAL if cls in {"OTHER", "EQH/EQL"} else SUPPORT,
                                     classification=cls, universal_rank=False,
                                     candidate_context="REVERSAL" if cls == "EXTERNAL" else "CONTINUATION" if cls == "INTERNAL" else "UNCLASSIFIED")
    # A direct sweep route cannot bypass an already known, subsequently broken reclaim.
    reactions = [es[r["child"]] for r in by_sweep[sw["event_id"]]
                 if r["type"] in {"RECLAIM_AFTER", "REJECTION_AFTER"} and es[r["child"]]["available_at"] <= d["timestamp"]]
    relevant = [parent] if parent["kind"] != "SWEPT" else reactions
    breaks = [{"reason": b, "available_at": d["available_at"], "availability_mode":"INHERITED_PROOF_UPPER_BOUND", "source": relation["relation_id"]}
              for b in proof.get("context_break", [])]
    for rec in relevant:
        price = sw["evidence"]["level_price"]
        for tf in sorted({rec["timeframe"], d["timeframe"]}):
            for c in candles.between(tf, rec["available_at"], d["timestamp"]):
                outside = c.close < price if d["direction"] == "BULLISH" else c.close > price
                if outside:
                    breaks.append({"reason": "RECLAIM_ACCEPTANCE_OUTSIDE", "timestamp": c.timestamp,
                                   "available_at": c.timestamp+TF[tf], "timeframe": tf, "close": c.close,
                                   "source": rec["event_id"]})
    # Explicit observed breaks are different from absence of downstream structure.
    contradiction = bool(breaks)
    f["RECLAIM_CONTINUITY"] = factor(CONTRADICT if contradiction else SUPPORT if relevant else NEUTRAL,
                                   reaction_ids=sorted(r["event_id"] for r in relevant), breaks=breaks)
    f["CONTEXT_CONTINUITY"] = factor(CONTRADICT if contradiction else SUPPORT, breaks=breaks)
    f["STRUCTURE_CONSEQUENCE"] = structure_evidence(graph, d, sw, relation)
    f["SUPERSESSION_STATE"] = factor(NEUTRAL, classification="UNRESOLVED")
    return {"attribution_id": uid(VERSION, relation["relation_id"]), "candidate_relation_id": relation["relation_id"],
            "displacement_id": d["event_id"], "sweep_id": sw["event_id"], "reaction_id": parent["event_id"] if parent["kind"] != "SWEPT" else None,
            "liquidity_id": level["event_id"], "factors": f,
            "contradictions": sorted(k for k in FACTORS if f[k]["state"] == CONTRADICT)}


def pair_state(es, old, new):
    a = es[old["sweep_id"]]; b = es[new["sweep_id"]]
    la = es[old["liquidity_id"]]; lb = es[new["liquidity_id"]]
    overlap = a["timestamp"] < b["available_at"] and b["timestamp"] < a["available_at"]
    same_identity = old["liquidity_id"] == new["liquidity_id"]
    same_price = la["evidence"]["price"] == lb["evidence"]["price"]
    breaks = [v for route in old["routes"] for v in route["factors"]["CONTEXT_CONTINUITY"]["evidence"].get("breaks", [])]
    superseded = (same_identity and not overlap and a["available_at"] <= b["timestamp"]
                  and old["contradicted"] and not new["contradicted"]
                  and any(v["available_at"] <= b["timestamp"] for v in breaks))
    ac = a["evidence"].get("candle", {}); bc = b["evidence"].get("candle", {})
    contained = bool(ac and bc and ((ac["low"] <= bc["low"] <= bc["high"] <= ac["high"])
                                  or (bc["low"] <= ac["low"] <= ac["high"] <= bc["high"])))
    nested = not same_price and contained and "INTERNAL" in {liquidity_class(la), liquidity_class(lb)}
    state = "SUPERSEDED_PARENT" if superseded else "NESTED_PARENT" if nested else "UNRESOLVED" if same_price or overlap else "PARALLEL_PARENT"
    return {"parent": old["sweep_id"], "other_parent": new["sweep_id"], "state": state,
            "same_liquidity_identity": same_identity, "same_cluster_exact_price": same_price,
            "timeframe_collision": a["timeframe"] != b["timeframe"], "timing_overlap": overlap,
            "nested_price_geometry": nested, "nearer_does_not_win": True}


def resolve_displacement(graph, routes):
    groups = defaultdict(list)
    for r in routes:
        groups[r["sweep_id"]].append(r)
    units = [{"sweep_id": k, "liquidity_id": rs[0]["liquidity_id"], "routes": sorted(rs, key=lambda r:r["attribution_id"]),
              "contradicted": all(r["contradictions"] for r in rs)} for k, rs in sorted(groups.items())]
    pairs = [pair_state(graph["events"], a, b) for a in units for b in units if a is not b]
    superseded = {p["parent"] for p in pairs if p["state"] == "SUPERSEDED_PARENT"}
    for unit in units:
        pp = [p for p in pairs if p["parent"] == unit["sweep_id"]]
        state = "SUPERSEDED_PARENT" if unit["sweep_id"] in superseded else "UNRESOLVED" if unit["contradicted"] else "NESTED_PARENT" if any(p["state"] == "NESTED_PARENT" for p in pp) else "ACTIVE_PARENT" if len(units)==1 else "PARALLEL_PARENT" if all(p["state"]=="PARALLEL_PARENT" for p in pp) else "UNRESOLVED"
        unit["supersession_state"] = state
        for route in unit["routes"]:
            route["factors"]["SUPERSESSION_STATE"] = factor(CONTRADICT if state=="SUPERSEDED_PARENT" else SUPPORT if state=="ACTIVE_PARENT" else NEUTRAL, classification=state)
            route["contradictions"] = sorted(k for k,v in route["factors"].items() if v["state"]==CONTRADICT)
        unit["contradicted"] = all(r["contradictions"] for r in unit["routes"])
    valid = [u for u in units if not u["contradicted"]]
    if len(valid)==1:
        valid[0]["supersession_state"]="ACTIVE_PARENT"
        for route in valid[0]["routes"]:
            route["factors"]["SUPERSESSION_STATE"]=factor(SUPPORT,classification="ACTIVE_PARENT")
    exact = len(valid)==1 and any(valid[0]["sweep_id"] in r["factors"]["STRUCTURE_CONSEQUENCE"]["evidence"].get("explicit_sweep_ids", [])
                                for r in valid[0]["routes"] if not r["contradictions"])
    state = "NO_VALID_PARENT" if not valid else "AMBIGUOUS_PARENT" if len(valid)>1 else "EXACT_PARENT" if exact else "UNAMBIGUOUS_PARENT"
    causes = set()
    if len(valid)>1:
        remaining = {u["sweep_id"] for u in valid}
        for pair in pairs:
            if pair["parent"] not in remaining or pair["other_parent"] not in remaining:
                continue
            for field, cause in (("same_cluster_exact_price","MULTIPLE_SAME_CLUSTER_SWEEPS"), ("nested_price_geometry","NESTED_SWEEPS"),
                                 ("timeframe_collision","CROSS_TIMEFRAME_PARENT_COLLISION"), ("timing_overlap","TIMING_OVERLAP")):
                if pair[field]: causes.add(cause)
        causes.update({"NO_PRICE_LOCALITY_SEPARATION", "NO_STRUCTURE_SEPARATION"})
    return {"resolution_state":state, "resolved_parent_id":valid[0]["sweep_id"] if len(valid)==1 else None,
            "candidate_parent_count":len(units), "valid_parent_count":len(valid), "parents":units,
            "parent_pairs":pairs, "ambiguity_causes":sorted(causes), "research_only":True}


def resolve_graph(graph, frames):
    cp=graph["origin_checkpoint"]; es=graph["events"]
    if any(e["available_at"]>cp for e in es.values()) or any(r["available_at"]>cp for r in graph["relations"]):
        raise ValueError("post-checkpoint evidence")
    index=ClosedIndex(frames,cp); by_parent=defaultdict(list)
    for r in graph["relations"]: by_parent[r["parent"]].append(r)
    routes=defaultdict(list)
    for r in graph["relations"]:
        if r["type"]=="DISPLACEMENT_CANDIDATE":
            routes[r["child"]].append(evaluate_route(graph,r,index,by_parent))
    resolved={k:resolve_displacement(graph,rs) for k,rs in sorted(routes.items())}
    mss=[]
    for source in graph["contextual_mss_candidates"]:
        parents=source["displacement_parents"]
        valid=[k for k in parents if k in resolved and resolved[k]["resolution_state"]!="NO_VALID_PARENT"]
        unambiguous=(len(parents)==1 and len(valid)==1 and resolved[valid[0]]["resolution_state"] in {"EXACT_PARENT","UNAMBIGUOUS_PARENT"})
        # Parent-only resolution does not secretly resolve competing structure/D children.
        mss.append({"mss_id":source["mss_id"], "raw_joint_ancestry_state":source["state"], "state":"UNAMBIGUOUS" if unambiguous else "AMBIGUOUS" if valid else "GENERIC_MSS",
                    "raw_displacement_parent_states":{k:es[k].get("parent_state") for k in parents},"displacement_ids":valid})
    chains=[]
    for chain in graph["chains"]:
        typ=chain["chain_type"]; branches=[]
        for k in chain["event_ids"]:
            if k not in resolved: continue
            rr=resolved[k]
            matching=[u for u in rr["parents"] if u["sweep_id"] in chain["event_ids"] and not u["contradicted"]]
            if not matching: continue
            structures=[r for r in by_parent[k] if r["type"]=="STRUCTURE_CONSEQUENCE_CANDIDATE" and es[r["child"]]["kind"]==("MSS" if typ=="REVERSAL" else "BOS")]
            zones=[r for r in by_parent[k] if r["type"] in {"FVG_CREATED_BY","OB_ASSOCIATED_WITH"}]
            reaction=any(r["reaction_id"] and not r["contradictions"] for u in matching for r in u["routes"])
            if structures and zones and (typ!="REVERSAL" or reaction):
                branches.append({"displacement_id":k,"unique_parent":rr["resolution_state"] in {"EXACT_PARENT","UNAMBIGUOUS_PARENT"},
                                 "unique_structure":all(s["ancestry_state"]!="AMBIGUOUS" for s in structures)})
        eligible_children=[k for k in chain["event_ids"] if k in resolved and any(not u["contradicted"] and u["sweep_id"] in chain["event_ids"] for u in resolved[k]["parents"])]
        state="PARTIAL" if not branches else "UNAMBIGUOUS" if len(branches)==1 and len(eligible_children)==1 and all(branches[0][k] for k in ("unique_parent","unique_structure")) else "AMBIGUOUS"
        chains.append({"candidate_chain_id":chain["candidate_chain_id"],"chain_type":typ,"state":state,"branches":branches})
    return {"schema":VERSION,"episode_id":graph["episode_id"],"origin_checkpoint":cp,
            "displacements":resolved,"contextual_mss":mss,"chains":chains,
            "candidate_relation_count":sum(len(rs) for rs in routes.values()),"outcome_access":False}
