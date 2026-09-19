"""Decision-only zone extensions and origin-level primary-chain states."""
from collections import defaultdict
from bisect import bisect_left
from hashlib import sha256
import json

from .raw_prefix_smc import TF
from .hybrid_attribution import AUTHORITATIVE
from .smc_ancestry_v11 import sweep_state

VERSION = "smc-hybrid-outcome.v1"
STAGES = ("LIQUIDITY_ONLY", "LIQUIDITY_TO_REACTION", "REACTION_TO_DISPLACEMENT",
          "DISPLACEMENT_TO_STRUCTURE", "STRUCTURE_TO_DEFENDABLE_ZONE", "FULL_OBSERVABLE_CHAIN")


def uid(kind, *parts):
    return kind + "-" + sha256(json.dumps([VERSION, *parts], sort_keys=True).encode()).hexdigest()[:24]


def bounds(e):
    v = e["evidence"]
    return (v["lower"], v["upper"]) if e["kind"] == "FVG" else (v["low"], v["high"])


class Prefix:
    def __init__(self, frames, checkpoint):
        self.candles = {tf: [c for c in f.closed_candles() if c.timestamp + TF[tf] <= checkpoint] for tf, f in frames.items()}
        self.times = {tf: [c.timestamp for c in cs] for tf, cs in self.candles.items()}
        self.checkpoint = checkpoint

    def after(self, tf, availability, end=None):
        cs = self.candles.get(tf, [])
        start = bisect_left(self.times.get(tf, []), availability)
        return [c for c in cs[start:] if c.timestamp + TF[tf] <= (self.checkpoint if end is None else end)]

    def zone(self, tf, lower, upper, direction, availability, end=None):
        bull = direction == "BULLISH"
        first, failure, defended, pending = None, None, None, None
        touches, entries, inside = 0, 0, False
        for c in self.after(tf, availability, end):
            ts = c.timestamp + TF[tf]
            if c.close < lower if bull else c.close > upper:
                failure = {"timestamp": c.timestamp, "available_at": ts, "close": c.close}
                break
            intersects = c.low <= upper and c.high >= lower
            away = c.close > upper if bull else c.close < lower
            if pending and away and defended is None:
                defended = {"retest_timestamp": pending, "timestamp": c.timestamp, "available_at": ts}
            if intersects:
                touches += 1
                entries += not inside
                if first is None:
                    first = {"timestamp": c.timestamp, "available_at": ts, "open": c.open, "high": c.high, "low": c.low, "close": c.close}
                if away:
                    pending = c.timestamp
            inside = intersects
        return {"status": "FAILED" if failure else "DEFENDED" if defended else "TESTED" if touches else "FRESH",
                "touch_count": touches, "mitigation_entry_count": entries, "first_retest": first,
                "defense": defended, "failure": failure, "as_of": end or self.checkpoint}


def extensions(graph, hybrid, frames):
    cp, es = graph["origin_checkpoint"], graph["events"]
    if any(e["available_at"] > cp for e in es.values()):
        raise ValueError("post-origin evidence")
    px = Prefix(frames, cp)
    creation, consequences = defaultdict(list), defaultdict(list)
    contextual = {x["structure_event_id"]: x for x in hybrid["structure_consequences"]}
    for r in graph["relations"]:
        if r["available_at"] > cp:
            raise ValueError("post-origin relation")
        if r["type"] in {"FVG_CREATED_BY", "OB_ASSOCIATED_WITH"}:
            creation[r["child"]].append(r["parent"])
        elif r["type"] == "STRUCTURE_CONSEQUENCE_CANDIDATE":
            consequences[r["parent"]].append(r["child"])
    def authoritative(did):
        return hybrid["parents"].get(did, {}).get("authority_type") in AUTHORITATIVE
    zones = {}
    for eid, e in sorted(es.items()):
        if e["kind"] not in {"FVG", "OB"}:
            continue
        low, high = bounds(e)
        ds = sorted(set(creation[eid]))
        zones[eid] = {"zone_id": eid, "type": e["kind"], "direction": e["direction"], "timeframe": e["timeframe"],
                      "lower": low, "upper": high, "creation_timestamp": e["timestamp"], "available_at": e["available_at"],
                      "displacement_ids": ds, "chain_linked": len(ds) == 1 and authoritative(ds[0]),
                      "detector_status": e["status"], "observation": px.zone(e["timeframe"], low, high, e["direction"], e["available_at"]),
                      "premium_discount": None, "distance_to_draw": None,
                      "location_unavailable_reason": "archived zone-specific range/target mapping absent"}
    reaction_children = defaultdict(list)
    for r in graph["relations"]:
        if r["type"] in {"RECLAIM_AFTER", "REJECTION_AFTER"}:
            reaction_children[r["parent"]].append(es[r["child"]])
    reactions = {eid: sweep_state(e, reaction_children[eid], px.candles[e["timeframe"]], cp)
                 for eid, e in es.items() if e["kind"] == "SWEPT"}
    bprs, old_by_tf = [], defaultdict(list)
    for newer in sorted((e for e in es.values() if e["kind"] == "FVG"), key=lambda e: (e["available_at"], e["event_id"])):
        tf = newer["timeframe"]
        nl, nu = bounds(newer)
        ds = creation[newer["event_id"]]
        candidates = []
        for old in old_by_tf[tf]:
            if old["direction"] == newer["direction"]:
                continue
            ol, ou = bounds(old)
            low, high = max(ol, nl), min(ou, nu)
            if low >= high:
                continue
            before = px.zone(tf, ol, ou, old["direction"], old["available_at"], es[ds[0]]["timestamp"]) if len(ds) == 1 else None
            plausible = len(ds) == 1 and authoritative(ds[0]) and old["available_at"] <= es[ds[0]]["timestamp"] and old["available_at"] < newer["available_at"] and before["failure"] is None
            candidates.append((old, low, high, plausible, before))
        plausible_count = sum(x[3] for x in candidates)
        for old, low, high, plausible, before in candidates:
            classification = "CHAIN_LINKED_BPR" if plausible and plausible_count == 1 else "AMBIGUOUS_BPR" if plausible or old["available_at"] == newer["available_at"] else "ISOLATED_BPR"
            bprs.append({"bpr_id": uid("BPR", old["event_id"], newer["event_id"]), "type": "BPR", "symbol": newer["symbol"],
                         "direction": newer["direction"], "timeframe": tf, "timeframes": [tf],
                         "source_fvg_ids": [old["event_id"], newer["event_id"]], "lower": low, "upper": high,
                         "creation_timestamp": newer["timestamp"], "available_at": newer["available_at"],
                         "classification": classification, "displacement_ids": ds, "plausible_older_sources": plausible_count,
                         "older_state_before_repricing": before, "observation": px.zone(tf, low, high, newer["direction"], newer["available_at"]),
                         "hybrid_parent_ids": [hybrid["parents"][d]["hybrid_parent_id"] for d in ds if d in hybrid["parents"]],
                         "premium_discount": None, "distance_to_draw": None, "opposing_liquidity_distance": None})
        old_by_tf[tf].append(newer)
    by_tf = defaultdict(list)
    for e in es.values():
        if e["kind"] == "DISPLACEMENT" and e.get("confirmation_evidence", {}).get("strength") in {"VALID", "STRONG"}:
            by_tf[e["timeframe"]].append(e)
    breakers = []
    for oid, z in zones.items():
        failure = z["observation"]["failure"]
        if z["type"] != "OB" or not failure:
            continue
        direction = "BEARISH" if z["direction"] == "BULLISH" else "BULLISH"
        low, high, tf = z["lower"], z["upper"], z["timeframe"]
        paths = []
        for d in sorted(by_tf[tf], key=lambda e: e["event_id"]):
            if d["direction"] != direction or d["timestamp"] < failure["available_at"]:
                continue
            candle = next((c for c in px.candles[tf] if c.timestamp == d["timestamp"]), None)
            if not candle or not (candle.low <= low and candle.high >= high):
                continue
            if not (candle.close > high if direction == "BULLISH" else candle.close < low):
                continue
            ss = consequences[d["event_id"]]
            if not ss:
                continue
            at = max(d["available_at"], max(es[s]["available_at"] for s in ss))
            obs = px.zone(tf, low, high, direction, at)
            retest = obs["first_retest"]
            reacted = retest is not None and (retest["close"] > high if direction == "BULLISH" else retest["close"] < low)
            unique_structure = all(contextual.get(s, {}).get("classification") == "CONTEXTUAL_" + es[s]["kind"] for s in ss)
            paths.append({"displacement_id": d["event_id"], "structure_ids": ss, "available_at": at,
                          "role_reaction": reacted, "observation": obs,
                          "causal_authority": authoritative(d["event_id"]) and unique_structure})
        confirmed = len(paths) == 1 and paths[0]["causal_authority"] and paths[0]["role_reaction"]
        generic = px.zone(tf, low, high, direction, failure["available_at"])
        retest = generic["first_retest"]
        generic_reaction = retest is not None and (retest["close"] > high if direction == "BULLISH" else retest["close"] < low)
        classification = "CONFIRMED_CHAIN_BREAKER" if confirmed else "BREAKER_CANDIDATE" if paths else "GENERIC_ROLE_REVERSAL" if generic_reaction else "NOT_CONFIRMED"
        breakers.append({"breaker_id": uid("BREAKER", oid, failure["timestamp"], direction), "type": "BREAKER", "source_ob_id": oid,
                         "failure_event": dict(failure, event_id=uid("OB_FAILURE", oid, failure["timestamp"])),
                         "direction": direction, "timeframe": tf, "lower": low, "upper": high, "paths": paths,
                         "classification": classification, "classification_as_of": cp,
                         "available_at": paths[0]["observation"]["first_retest"]["available_at"] if confirmed else retest["available_at"] if classification == "GENERIC_ROLE_REVERSAL" else max([failure["available_at"]] + [p["available_at"] for p in paths]),
                         "observation": paths[0]["observation"] if len(paths) == 1 else generic,
                         "displacement_ids": [p["displacement_id"] for p in paths]})
    return {"schema": VERSION, "episode_id": graph["episode_id"], "checkpoint": cp,
            "zones": zones, "bprs": bprs, "breakers": breakers, "reactions": reactions, "outcome_access": False}


def choose_zone(items, label):
    if not items:
        return {"state": "NONE", "ids": []}
    latest = max(z["available_at"] for z in items)
    chosen = [z for z in items if z["available_at"] == latest]
    states = sorted({label(z) for z in chosen})
    return {"state": states[0] if len(states) == 1 else "MULTIPLE_RELEVANT_ZONES", "ids": sorted(z.get("zone_id", z.get("bpr_id", z.get("breaker_id"))) for z in chosen),
            "observations": [z["observation"] for z in chosen]}


def freeze_origin(graph, hybrid, ext, origin):
    """This interface accepts only decision-time features, never outcomes/regime labels."""
    es, cp = graph["events"], graph["origin_checkpoint"]
    if origin["checkpoint"] != cp or origin["episode_id"] != graph["episode_id"]:
        raise ValueError("origin identity mismatch")
    bias = "BULLISH" if origin["direction"] == "LONG" else "BEARISH"
    phase = origin["phase"]
    profile = "REVERSAL" if phase in {"REVERSAL_CANDIDATE", "EXHAUSTION"} else "CONTINUATION" if phase in {"CONTINUATION", "PULLBACK"} else None
    protected = origin["families"]["DEFENSE_QUALITY"]["evidence"]["protected_levels"]
    prices = {float(v) for v in protected.values() if v not in {None, "NONE"}}
    room = origin["families"]["REMAINING_ROOM_CONTEXT"]["evidence"]
    raw_chains = {c["candidate_chain_id"]: c for c in graph["chains"]}
    structural = {r["structure_event_id"]: r for r in hybrid["structure_consequences"]}
    candidates = {}
    for hc in hybrid["chains"]:
        raw = raw_chains[hc["source_candidate_chain_id"]]
        if profile and hc["chain_type"] != profile:
            continue
        ids = set(raw["event_ids"])
        sweeps = sorted(s for s in ids if es[s]["kind"] == "SWEPT" and es[s]["direction"] == bias)
        if not sweeps:
            continue
        ds = sorted(d for d in ids if d in hybrid["parents"] and es[d]["direction"] == bias)
        for did in ds or [None]:
            p = hybrid["parents"].get(did)
            valid = p and p["authority_type"] in AUTHORITATIVE
            units = [u for u in p["relationship_evidence"]["eligible_parent_units"] if valid or u["sweep_id"] in sweeps] if p else []
            member_sweeps = p["member_parent_event_ids"] if valid else sweeps
            reactions = sorted({r["reaction_id"] for u in units for r in u["routes"] if r["reaction_id"] and not r["contradictions"]})
            ss = sorted(s for s in ids if s in structural and did in structural[s]["displacement_ids"])
            contextual = [s for s in ss if structural[s]["classification"].startswith("CONTEXTUAL_")]
            linked = [z for z in ext["zones"].values() if z["direction"] == bias and z["chain_linked"] and did in z["displacement_ids"]]
            linked += [z for z in ext["bprs"] if z["direction"] == bias and z["classification"] == "CHAIN_LINKED_BPR" and did in z["displacement_ids"]]
            linked += [z for z in ext["breakers"] if z["direction"] == bias and z["classification"] == "CONFIRMED_CHAIN_BREAKER" and did in z["displacement_ids"]]
            usable = [z for z in linked if z["observation"]["status"] != "FAILED"]
            known_reaction = any(ext["reactions"][s][0] in {"SWEEP_RECLAIMED", "SWEEP_REJECTED", "SWEEP_ACCEPTED_OUTSIDE"} for s in member_sweeps)
            stage = "LIQUIDITY_TO_REACTION" if reactions or known_reaction else "LIQUIDITY_ONLY"
            if valid:
                stage = "REACTION_TO_DISPLACEMENT"
            if valid and contextual:
                stage = "DISPLACEMENT_TO_STRUCTURE"
                if usable:
                    stage = "STRUCTURE_TO_DEFENDABLE_ZONE"
                    if reactions and any(z["observation"]["status"] == "DEFENDED" for z in usable) and room.get("remaining_room_pct") is not None and room["remaining_room_pct"] > 0:
                        stage = "FULL_OBSERVABLE_CHAIN"
            relevant = any(es[s]["evidence"].get("price") in prices for s in ss)
            latest = max(es[e]["available_at"] for e in [*sweeps, *reactions, *ss, *([did] if did else [])])
            key = (hc["chain_type"], p["hybrid_parent_id"] if p else raw["candidate_chain_id"], did)
            if key in candidates:
                candidates[key]["chain_ids"].append(hc["hybrid_chain_id"])
                continue
            candidates[key] = {"chain_ids": [hc["hybrid_chain_id"]], "chain_type": hc["chain_type"], "displacement_id": did,
                               "hybrid_parent_id": p["hybrid_parent_id"] if p else None,
                               "authority": p["authority_type"] if p else "NO_VALID_PARENT", "sweep_ids": member_sweeps,
                               "reaction_ids": reactions, "structure_ids": ss, "contextual_ids": contextual,
                               "stage": stage, "structural_relevance": relevant, "latest_available_at": latest}
    chosen = list(candidates.values())
    trace = {"direction_phase_compatible": len(chosen)}
    if any(c["structural_relevance"] for c in chosen):
        chosen = [c for c in chosen if c["structural_relevance"]]
    trace["structurally_relevant"] = len(chosen)
    if chosen:
        deepest = next(s for s in reversed(STAGES) if any(c["stage"] == s for c in chosen))
        chosen = [c for c in chosen if c["stage"] == deepest]
        trace["deepest_stage"] = len(chosen)
        latest = max(c["latest_available_at"] for c in chosen)
        chosen = [c for c in chosen if c["latest_available_at"] == latest]
    primary = chosen[0] if len(chosen) == 1 else None
    did = primary["displacement_id"] if primary else None
    fields = {"chain_type": primary["chain_type"] if primary else "MULTIPLE_RELEVANT_CHAINS" if chosen else "NO_RELEVANT_CHAIN",
              "completeness": primary["stage"] if primary else "UNRESOLVED", "parent_authority": primary["authority"] if primary else "UNRESOLVED",
              "location": origin["families"]["PREMIUM_DISCOUNT_LOCATION"]["evidence"]["location"],
              "extension": "EXTENDED" if "HIGH_EXTENSION" in room.get("risk_signatures", []) else "NOT_FLAGGED",
              "remaining_room": "UNAVAILABLE" if room.get("remaining_room_pct") is None else "OBSERVED",
              "trend_maturity": room.get("trend_maturity", "UNAVAILABLE")}
    for kind in ("MSS", "BOS"):
        ss = [es[s] for s in (primary["structure_ids"] if primary else []) if es[s]["kind"] == kind]
        fields[kind] = "CONTEXTUAL" if any(structural[e["event_id"]]["classification"] == "CONTEXTUAL_" + kind for e in ss) else "GENERIC" if ss else "GENERIC_INVENTORY" if any(e["kind"] == kind and e["direction"] == bias for e in es.values()) else "NONE"
    reaction_states = {ext["reactions"][s][0] for s in primary["sweep_ids"]} if primary else set()
    fields["reaction"] = "+".join(sorted(reaction_states)) if reaction_states else "UNRESOLVED"
    fields["reaction_divergence"] = "YES" if "SWEEP_ACCEPTED_OUTSIDE" in reaction_states and reaction_states & {"SWEEP_RECLAIMED", "SWEEP_REJECTED"} else "NO"
    fields["liquidity_context"] = primary["chain_type"] if primary else "UNRESOLVED"
    selected_zones = {}
    for kind in ("FVG", "OB"):
        relevant = [z for z in ext["zones"].values() if z["type"] == kind and z["direction"] == bias]
        linked = [z for z in relevant if z["chain_linked"] and did is not None and did in z["displacement_ids"]]
        selected_zones[kind] = choose_zone(linked or relevant, lambda z: ("CHAIN_LINKED_" if linked else "OTHER_CHAIN_" if z["chain_linked"] else "ISOLATED_") + z["observation"]["status"])
        fields[kind] = selected_zones[kind]["state"]
    for kind, key in (("BPR", "bprs"), ("BREAKER", "breakers")):
        relevant = [z for z in ext[key] if z["direction"] == bias]
        linked = [z for z in relevant if did is not None and did in z["displacement_ids"]]
        selected_zones[kind] = choose_zone(linked or relevant, lambda z: ("OTHER_" if not linked and z["classification"] in {"CHAIN_LINKED_BPR", "CONFIRMED_CHAIN_BREAKER"} else "") + z["classification"])
        fields[kind] = selected_zones[kind]["state"]
        fields[kind + "_retest"] = "NONE" if not selected_zones[kind].get("observations") else "REPEATED" if any(z["mitigation_entry_count"] > 1 for z in selected_zones[kind]["observations"]) else "FIRST" if any(z["mitigation_entry_count"] == 1 for z in selected_zones[kind]["observations"]) else "UNTESTED"
    fields["FVG_linkage"] = "CHAIN_LINKED" if fields["FVG"].startswith("CHAIN_LINKED_") else "ISOLATED" if fields["FVG"].startswith("ISOLATED_") else "OTHER_OR_UNRESOLVED"
    fields["FVG_vs_BPR"] = "CHAIN_FVG_PLUS_BPR" if fields["BPR"] == "CHAIN_LINKED_BPR" else "CHAIN_FVG_NO_BPR" if fields["FVG_linkage"] == "CHAIN_LINKED" else fields["FVG_linkage"]
    fields["OB_vs_BREAKER"] = "CONFIRMED_BREAKER" if fields["BREAKER"] == "CONFIRMED_CHAIN_BREAKER" else "FAILED_OB_WITHOUT_CONFIRMED_BREAKER" if fields["OB"].endswith("_FAILED") else "OTHER_OB_CONTEXT"
    return {"schema": VERSION, "episode_id": graph["episode_id"], "checkpoint": cp, "direction": origin["direction"],
            "symbol": origin["symbol"], "phase": phase, "selection_trace": trace, "primary": primary,
            "unresolved_candidates": chosen if not primary else [], "fields": fields, "selected_zones": selected_zones,
            "existing_context": {k: v["state"] for k, v in origin["families"].items()},
            "remaining_room_metrics": room, "outcome_access": False}
