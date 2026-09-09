"""Outcome-blind liquidity interaction clusters; never a production authority."""
from bisect import bisect_right
from collections import defaultdict
from hashlib import sha256
import json

from .raw_prefix_smc import TF
from .parent_attribution import ClosedIndex, liquidity_class
from .smc_ancestry_v11 import deadline, sweep_state, validate_reaction

VERSION = "smc-liquidity-cluster.v1"


def identity(kind, *parts):
    body = json.dumps([VERSION, kind, *parts], sort_keys=True, separators=(",", ":"))
    return kind + "-" + sha256(body.encode()).hexdigest()[:24]


def composition(level):
    text = level["evidence"].get("type", "").upper()
    names = [k for k in ("PDH", "PDL", "PWH", "PWL", "INTERNAL", "EXTERNAL") if k in text]
    if "EQUAL" in text:
        names.append("EQH" if level["direction"] == "BEARISH" else "EQL")
    if not names or set(names) <= {"INTERNAL", "EXTERNAL"}:
        names.append("SWING_HIGH" if level["direction"] == "BEARISH" else "SWING_LOW")
    return sorted(names)


def contains(a, b):
    return (a["low"] <= b["low"] <= b["high"] <= a["high"]
            and (a["low"], a["high"]) != (b["low"], b["high"]))


class ClusterBuilder:
    def __init__(self, graph, frames):
        self.graph = graph
        self.events = graph["events"]
        self.checkpoint = graph["origin_checkpoint"]
        if any(e["available_at"] > self.checkpoint or e["timestamp"] + TF[e["timeframe"]] > e["available_at"] for e in self.events.values()):
            raise ValueError("future or open event")
        if any(r["available_at"] > self.checkpoint for r in graph["relations"]):
            raise ValueError("future relation")
        self.candles = ClosedIndex(frames, self.checkpoint)
        self.references = {}
        self.reactions = defaultdict(list)
        self.children = defaultdict(list)
        for r in graph["relations"]:
            self.children[r["parent"]].append(r)
            if r["type"] == "SWEEP_OF":
                self.references[r["child"]] = r["parent"]
            elif r["type"] in {"RECLAIM_AFTER", "REJECTION_AFTER"}:
                self.reactions[r["parent"]].append(self.events[r["child"]])
        self.sweeps = sorted((self.events[k] for k in self.references), key=lambda e: (e["timestamp"], e["event_id"]))
        self.opposing = defaultdict(list)
        for e in self.events.values():
            if e["kind"] in {"BOS", "MSS"} or (e["kind"] == "DISPLACEMENT" and e.get("confirmation_evidence", e["evidence"]).get("strength") in {"VALID", "STRONG"}):
                self.opposing[(e["symbol"], e["timeframe"], e["direction"])].append(e)
        for rows in self.opposing.values():
            rows.sort(key=lambda e: (e["available_at"], e["event_id"]))
        self.opposing_times = {k: [e["available_at"] for e in rows] for k, rows in self.opposing.items()}
        self.pair_cache = {}
        self.membership_cache = {}
        self.views = {}
        self.peak_pair_checks = 0

    def breaks(self, sweep, end):
        """Existing extreme/acceptance semantics, using only fully closed evidence."""
        start = sweep["available_at"]
        if end <= start:
            return []
        tf = sweep["timeframe"]
        opposite = "BEARISH" if sweep["direction"] == "BULLISH" else "BULLISH"
        key = sweep["symbol"], tf, opposite
        times = self.opposing_times.get(key, [])
        result = [{"reason": "OPPOSING_" + e["kind"], "event_id": e["event_id"], "available_at": e["available_at"]}
                  for e in self.opposing.get(key, [])[bisect_right(times, start):bisect_right(times, end)]
                  if e["timestamp"] >= start]
        reactions = [r for r in self.reactions[sweep["event_id"]] if r["available_at"] <= end and validate_reaction(sweep, r)]
        first_inside = min((r["available_at"] for r in reactions), default=None)
        for c in self.candles.between(tf, start, end):
            bull = sweep["direction"] == "BULLISH"
            outside_extreme = c.close < sweep["evidence"]["sweep_price"] if bull else c.close > sweep["evidence"]["sweep_price"]
            outside_level = c.close < sweep["evidence"]["level_price"] if bull else c.close > sweep["evidence"]["level_price"]
            reason = "SWEEP_EXTREME_INVALIDATED" if outside_extreme else "RECLAIM_ACCEPTANCE_OUTSIDE" if first_inside is not None and c.timestamp >= first_inside and outside_level else None
            if reason:
                result.append({"reason": reason, "timestamp": c.timestamp, "available_at": c.timestamp + TF[tf], "timeframe": tf, "close": c.close})
        return sorted(result, key=lambda b: (b["available_at"], b["reason"], b.get("event_id", "")))

    def compatible(self, a, b):
        ids = tuple(sorted((a["event_id"], b["event_id"])))
        if ids in self.pair_cache:
            return self.pair_cache[ids]
        la = self.events[self.references[a["event_id"]]]
        lb = self.events[self.references[b["event_id"]]]
        ca, cb = a["evidence"].get("candle"), b["evidence"].get("candle")
        overlap = max(a["timestamp"], b["timestamp"]) < min(a["available_at"], b["available_at"])
        same_price = la["evidence"]["price"] == lb["evidence"]["price"]
        nested = bool(ca and cb and (contains(ca, cb) or contains(cb, ca))
                      and "INTERNAL" in {liquidity_class(la), liquidity_class(lb)}
                      and ca["low"] <= lb["evidence"]["price"] <= ca["high"]
                      and cb["low"] <= la["evidence"]["price"] <= cb["high"])
        reasons = []
        if a["symbol"] != b["symbol"]: reasons.append("SYMBOL_MISMATCH")
        if a["direction"] != b["direction"]: reasons.append("LIQUIDITY_SIDE_MISMATCH")
        if not overlap: reasons.append("NO_OVERLAPPING_INTERACTION")
        if not (same_price or nested): reasons.append("NO_STRUCTURAL_PRICE_ZONE")
        at = max(a["available_at"], b["available_at"])
        breaks = self.breaks(a, at) + self.breaks(b, at) if not reasons else []
        if breaks: reasons.append("CONTEXT_BREAK")
        proof = {"member_sweep_ids": list(ids), "compatible": not reasons, "reasons": reasons,
                 "available_at": at, "same_reference_identity": la["event_id"] == lb["event_id"],
                 "same_reference_price": same_price, "nested_geometry": nested,
                 "overlapping_interaction": overlap, "context_breaks": breaks}
        self.pair_cache[ids] = proof
        return proof

    def membership(self, cutoff):
        visible = [e for e in self.sweeps if e["available_at"] <= cutoff]
        key = tuple(e["event_id"] for e in visible)
        if key in self.membership_cache:
            return self.membership_cache[key]
        adj = {e["event_id"]: set() for e in visible}
        active = defaultdict(list)
        checked = 0
        for e in visible:
            group = e["symbol"], e["direction"]
            prior = [p for p in active[group] if p["available_at"] > e["timestamp"]]
            for p in prior:
                checked += 1
                if self.compatible(p, e)["compatible"]:
                    adj[p["event_id"]].add(e["event_id"])
                    adj[e["event_id"]].add(p["event_id"])
            active[group] = prior + [e]
        self.peak_pair_checks = max(self.peak_pair_checks, checked)
        remaining = set(adj)
        groups = []
        while remaining:
            todo = [min(remaining)]
            component = set()
            while todo:
                k = todo.pop()
                if k not in component:
                    component.add(k)
                    todo.extend(adj[k] - component)
            remaining -= component
            # Never let transitive proximity merge incompatible end-points; no greedy winner.
            clique = all(component - {k} <= adj[k] for k in component)
            if clique:
                groups.append((sorted(component), []))
            else:
                groups.extend(([k], sorted(component - {k})) for k in sorted(component))
        groups.sort(key=lambda x: x[0])
        self.membership_cache[key] = groups
        return groups

    def cluster(self, members, alternatives, cutoff):
        es = self.events
        sweeps = [es[k] for k in members]
        lids = sorted({self.references[k] for k in members})
        classes = {liquidity_class(es[k]) for k in lids}
        pair_proofs = [self.compatible(a, b) for i, a in enumerate(sweeps) for b in sweeps[i+1:]]
        nested = any(p["nested_geometry"] and not p["same_reference_price"] for p in pair_proofs)
        typ = ("UNRESOLVED_CLUSTER" if alternatives else "NESTED_LIQUIDITY_CLUSTER" if nested else
               "EXTERNAL_LIQUIDITY_CLUSTER" if classes == {"EXTERNAL"} else
               "INTERNAL_LIQUIDITY_CLUSTER" if classes == {"INTERNAL"} else
               "MIXED_LIQUIDITY_CLUSTER" if len(classes) > 1 else "UNRESOLVED_CLUSTER")
        depths = {}
        for sw in sorted(sweeps, key=lambda e: (-(e["evidence"].get("candle", {}).get("high", 0)-e["evidence"].get("candle", {}).get("low", 0)), e["event_id"])):
            c = sw["evidence"].get("candle")
            parents = [x for x in depths if c and es[x]["evidence"].get("candle") and contains(es[x]["evidence"]["candle"], c)
                       and es[x]["evidence"]["level_price"] != sw["evidence"]["level_price"]]
            depths[sw["event_id"]] = 1 + max((depths[k] for k in parents), default=-1)
        reactions = sorted({r["event_id"] for k in members for r in self.reactions[k] if r["available_at"] <= cutoff and validate_reaction(es[k], r)})
        excluded_reactions = sorted({r["event_id"] for k in members for r in self.reactions[k] if r["available_at"] <= cutoff and not validate_reaction(es[k], r)})
        states = []
        failures = []
        for sw in sweeps:
            state, at = sweep_state(sw, self.reactions[sw["event_id"]], self.candles.rows.get(sw["timeframe"], []), cutoff)
            states.append({"sweep_id": sw["event_id"], "state": state, "available_at": at})
            failures.extend(dict(b, sweep_id=sw["event_id"]) for b in self.breaks(sw, min(cutoff, deadline(sw))))
        kinds = {s["state"] for s in states}
        resolution = ("REJECTION_RESOLUTION" if kinds == {"SWEEP_REJECTED"} else
                      "RECLAIM_RESOLUTION" if kinds <= {"SWEEP_RECLAIMED", "SWEEP_REJECTED"} else
                      "ACCEPTANCE_RESOLUTION" if kinds == {"SWEEP_ACCEPTED_OUTSIDE"} else "UNRESOLVED")
        at = max((s["available_at"] or 0 for s in states), default=0) if resolution != "UNRESOLVED" else None
        lifecycle = "INVALIDATED" if failures else {"RECLAIM_RESOLUTION": "RECLAIMED", "REJECTION_RESOLUTION": "REJECTED", "ACCEPTANCE_RESOLUTION": "ACCEPTED_OUTSIDE"}.get(resolution, "SWEPT")
        cid = identity("LIC", sweeps[0]["symbol"], sweeps[0]["direction"], lids, sorted(members))
        return {"liquidity_interaction_cluster_id": cid, "cluster_view_id": identity("LICVIEW", cid, cutoff),
                "as_of": cutoff, "membership_available_at": max(e["available_at"] for e in sweeps),
                "symbol": sweeps[0]["symbol"], "direction": sweeps[0]["direction"],
                "liquidity_side": "SELLSIDE" if sweeps[0]["direction"] == "BULLISH" else "BUYSIDE",
                "cluster_type": typ, "member_sweep_ids": sorted(members), "liquidity_reference_ids": lids,
                "reference_composition": sorted({x for k in lids for x in composition(es[k])}),
                "reaction_ids": reactions, "excluded_reaction_ids": excluded_reactions,
                "member_resolution_states": states, "nested_depth": depths,
                "first_sweep_time": min(e["timestamp"] for e in sweeps), "last_sweep_time": max(e["timestamp"] for e in sweeps),
                "membership_proofs": pair_proofs, "unresolved_membership_alternatives": alternatives,
                "resolution": resolution, "resolution_available_at": at, "lifecycle_state": lifecycle,
                "context_breaks": failures, "candidate_only": True}

    def view(self, cutoff, targets=None):
        if cutoff > self.checkpoint:
            raise ValueError("post-origin cluster view")
        result = []
        for m, a in self.membership(cutoff):
            if targets is not None and not set(m).intersection(targets):
                continue
            key = cutoff, tuple(m)
            if key not in self.views:
                self.views[key] = self.cluster(m, a, cutoff)
            result.append(self.views[key])
        return result


def reconstruct(graph, parent_result, frames):
    builder = ClusterBuilder(graph, frames)
    es = graph["events"]
    if parent_result["episode_id"] != graph["episode_id"] or parent_result["origin_checkpoint"] != graph["origin_checkpoint"]:
        raise ValueError("parent authority mismatch")
    current = builder.view(graph["origin_checkpoint"])
    used_views = {}
    displacements = {}
    for did, prior in sorted(parent_result["displacements"].items()):
        d = es[did]
        targets = {u["sweep_id"] for u in prior["parents"]}
        by_sweep = {sid: c for c in builder.view(d["timestamp"], targets) for sid in c["member_sweep_ids"]}
        candidates = defaultdict(list)
        for unit in prior["parents"]:
            valid_routes = [r for r in unit["routes"] if not r["contradictions"]]
            if not valid_routes or unit["sweep_id"] not in by_sweep:
                continue
            c = by_sweep[unit["sweep_id"]]
            used_views[c["cluster_view_id"]] = c
            candidates[c["cluster_view_id"]].extend(valid_routes)
        relations = []
        for vid, routes in sorted(candidates.items()):
            c = used_views[vid]
            reasons = []
            if c["direction"] != d["direction"] or c["symbol"] != d["symbol"]: reasons.append("DIRECTION_SYMBOL_MISMATCH")
            if c["membership_available_at"] > d["timestamp"]: reasons.append("FUTURE_MEMBERSHIP")
            if c["context_breaks"]: reasons.append("CLUSTER_CONTEXT_BREAK")
            if c["resolution"] == "ACCEPTANCE_RESOLUTION": reasons.append("ACCEPTED_OUTSIDE")
            if c["unresolved_membership_alternatives"]: reasons.append("UNRESOLVED_MEMBERSHIP")
            relations.append({"relationship_id": identity("REL", "DISPLACEMENT_AFTER_CLUSTER", vid, did),
                              "relationship_type": "DISPLACEMENT_AFTER_CLUSTER", "cluster_view_id": vid,
                              "liquidity_interaction_cluster_id": c["liquidity_interaction_cluster_id"],
                              "displacement_id": did, "available_at": d["available_at"],
                              "supporting_route_ids": sorted(r["candidate_relation_id"] for r in routes),
                              "supporting_sweep_ids": sorted({r["sweep_id"] for r in routes}),
                              "evidence_factors": {k: [r["factors"][k] for r in routes] for k in
                                  ("DIRECTION_COMPATIBILITY", "TEMPORAL_PROXIMITY", "PRICE_LOCALITY", "CONTEXT_CONTINUITY", "STRUCTURE_CONSEQUENCE")},
                              "eligible": not reasons, "reasons": reasons})
        valid = [r for r in relations if r["eligible"]]
        # Membership uncertainty must not masquerade as unique causal ancestry.
        unresolved = any(r["reasons"] == ["UNRESOLVED_MEMBERSHIP"] for r in relations)
        state = "AMBIGUOUS_CLUSTER_PARENT" if len(valid) > 1 or unresolved else "UNAMBIGUOUS_CLUSTER_PARENT" if len(valid) == 1 else "NO_CLUSTER_PARENT"
        displacements[did] = {"previous_single_sweep_state": prior["resolution_state"], "state": state,
                              "resolved_cluster_view_id": valid[0]["cluster_view_id"] if state == "UNAMBIGUOUS_CLUSTER_PARENT" else None,
                              "relations": relations}
    structural = []
    structure_parents = defaultdict(list)
    for r in graph["relations"]:
        if r["type"] == "STRUCTURE_CONSEQUENCE_CANDIDATE": structure_parents[r["child"]].append(r)
    for sid in sorted(k for k, e in es.items() if e["kind"] in {"BOS", "MSS"}):
        edges = structure_parents[sid]
        linked = [r for r in edges if r["parent"] in displacements and displacements[r["parent"]]["state"] != "NO_CLUSTER_PARENT"]
        unique = len(edges) == 1 and len(linked) == 1 and displacements[linked[0]["parent"]]["state"] == "UNAMBIGUOUS_CLUSTER_PARENT"
        structural.append({"structure_event_id": sid, "kind": es[sid]["kind"],
                           "classification": "CONTEXTUAL_" + es[sid]["kind"] + "_AFTER_CLUSTER" if unique else "AMBIGUOUS_CONTEXTUAL_CANDIDATE" if linked else "GENERIC_" + es[sid]["kind"],
                           "displacement_ids": sorted(r["parent"] for r in linked), "structure_proofs": [r["proof"] for r in linked],
                           "available_at": max([es[sid]["available_at"]] + [r["available_at"] for r in linked])})
    contextual = {r["structure_event_id"]: r for r in structural}
    chains = []
    for did, d in displacements.items():
        descendants = builder.children[did]
        zones = [r for r in descendants if r["type"] in {"FVG_CREATED_BY", "OB_ASSOCIATED_WITH"}]
        for r in d["relations"]:
            c = used_views[r["cluster_view_id"]]
            scopes = c["reference_composition"]
            profiles = [p for label, p in (("EXTERNAL", "REVERSAL"), ("INTERNAL", "CONTINUATION")) if label in scopes] or ["UNCLASSIFIED"]
            for profile in profiles:
                structures = [s for s in descendants if s["type"] == "STRUCTURE_CONSEQUENCE_CANDIDATE" and es[s["child"]]["kind"] == ("MSS" if profile == "REVERSAL" else "BOS")]
                resolved = c["resolution"] in {"RECLAIM_RESOLUTION", "REJECTION_RESOLUTION"} and c["resolution_available_at"] <= es[did]["timestamp"]
                complete = r["eligible"] and resolved and bool(structures and zones)
                unique = d["state"] == "UNAMBIGUOUS_CLUSTER_PARENT" and all(contextual[s["child"]]["classification"].startswith("CONTEXTUAL_") for s in structures)
                chains.append({"cluster_chain_id": identity("LICCHAIN", profile, r["cluster_view_id"], did), "profile": profile,
                               "state": "UNAMBIGUOUS_CHAIN_CANDIDATE" if complete and unique else "AMBIGUOUS_CHAIN_CANDIDATE" if complete else "PARTIAL_CHAIN",
                               "cluster_view_id": r["cluster_view_id"], "displacement_id": did,
                               "resolution": c["resolution"], "resolution_available_at": c["resolution_available_at"],
                               "structure_event_ids": sorted(s["child"] for s in structures), "descendant_zone_ids": sorted(z["child"] for z in zones),
                               "event_relation_ids": sorted([s["relation_id"] for s in structures] + [z["relation_id"] for z in zones]),
                               "candidate_only": True})
    return {"schema": VERSION, "episode_id": graph["episode_id"], "origin_checkpoint": graph["origin_checkpoint"],
            "clusters": current, "parent_cluster_views": dict(sorted(used_views.items())), "displacements": displacements,
            "structure_consequences": structural, "chains": sorted(chains, key=lambda c: c["cluster_chain_id"]),
            "peak_membership_pair_checks": builder.peak_pair_checks, "outcome_access": False,
            "authority": "RAW_PREFIX_RESEARCH", "previous_event_ids_unchanged": True}
