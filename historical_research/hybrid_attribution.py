"""Outcome-blind hybrid authority over frozen event and clique provenance."""
from collections import defaultdict
from hashlib import sha256
from itertools import combinations
import json

VERSION = "smc-hybrid-attribution.v1"
AUTHORITATIVE = {"EXACT_SINGLE_EVENT_PARENT", "UNAMBIGUOUS_SINGLE_EVENT_PARENT", "COHERENT_CLUSTER_PARENT"}


def identity(kind, *parts):
    payload = json.dumps([VERSION, *parts], sort_keys=True, separators=(",", ":"))
    return kind + "-" + sha256(payload.encode()).hexdigest()[:24]


def divergence(cluster):
    states = {r["state"] for r in cluster["member_resolution_states"]}
    return "ACCEPTED_OUTSIDE" in " ".join(sorted(states)) and any(
        "RECLAIMED" in s or "REJECTED" in s for s in states)


def grouping_reasons(cluster, displacement, events):
    reasons = []
    if cluster["unresolved_membership_alternatives"]:
        reasons.append("UNRESOLVED_MEMBERSHIP")
    if cluster["context_breaks"]:
        reasons.append("CONTEXT_BREAK")
    if divergence(cluster):
        reasons.append("REACTION_DIVERGENCE")
    if cluster["resolution"] == "ACCEPTANCE_RESOLUTION":
        reasons.append("CONTRADICTORY_REACTION")
    if cluster["as_of"] > displacement["timestamp"] or cluster["membership_available_at"] > displacement["timestamp"]:
        reasons.append("INVALID_TEMPORAL_ORDER")
    for sid in cluster["member_sweep_ids"]:
        e = events[sid]
        if e["symbol"] != displacement["symbol"] or e["direction"] != displacement["direction"]:
            reasons.append("SIDE_OR_DIRECTION_CONTRADICTION")
        if e["available_at"] > displacement["timestamp"]:
            reasons.append("INVALID_TEMPORAL_ORDER")
    proofs = {tuple(sorted(p["member_sweep_ids"])): p for p in cluster["membership_proofs"]}
    for pair in combinations(sorted(cluster["member_sweep_ids"]), 2):
        p = proofs.get(pair)
        if not p or not p["compatible"] or p["available_at"] > displacement["timestamp"]:
            reasons.append("PAIRWISE_MEMBERSHIP_UNPROVEN")
    return sorted(set(reasons))


def resolve(graph, single, clusters):
    """No candle detector, outcome loader, production writer, or parent ranking."""
    es, checkpoint = graph["events"], graph["origin_checkpoint"]
    if any(e["available_at"] > checkpoint for e in es.values()):
        raise ValueError("post-checkpoint event")
    children = defaultdict(list)
    structure_parents = defaultdict(list)
    for r in graph["relations"]:
        if r["available_at"] > checkpoint:
            raise ValueError("post-checkpoint relation")
        children[r["parent"]].append(r)
        if r["type"] == "STRUCTURE_CONSEQUENCE_CANDIDATE":
            structure_parents[r["child"]].append(r)
    parents = {}
    for did, prior in sorted(single["displacements"].items()):
        d = es[did]
        old = clusters["displacements"][did]
        views = {r["cluster_view_id"]: clusters["parent_cluster_views"][r["cluster_view_id"]] for r in old["relations"]}
        plausible, rejected = [], []
        for unit in prior["parents"]:
            sid = unit["sweep_id"]
            direct = []
            if unit["contradicted"]:
                direct.append({"reason": "FROZEN_SINGLE_EVENT_CONTRADICTION", "routes": unit["routes"]})
            if es[sid]["symbol"] != d["symbol"] or es[sid]["direction"] != d["direction"]:
                direct.append({"reason": "SYMBOL_OR_DIRECTION_CONTRADICTION"})
            if es[sid]["available_at"] > d["timestamp"]:
                direct.append({"reason": "INVALID_TEMPORAL_ORDER"})
            # A different cluster member's failure is not evidence against this parent.
            for c in views.values():
                for b in c["context_breaks"]:
                    if b.get("sweep_id") == sid and b.get("available_at", checkpoint + 1) <= d["timestamp"]:
                        direct.append(b)
            if direct:
                rejected.append({"sweep_id": sid, "direct_contradictions": direct})
            else:
                plausible.append(unit)
        ids = sorted(u["sweep_id"] for u in plausible)
        assessments = [{"cluster_view_id": vid, "cluster_id": c["liquidity_interaction_cluster_id"],
                        "member_sweep_ids": c["member_sweep_ids"],
                        "covers_all_parents": bool(ids) and set(ids) <= set(c["member_sweep_ids"]),
                        "reasons": grouping_reasons(c, d, es)} for vid, c in sorted(views.items())]
        proven = [a for a in assessments if a["covers_all_parents"] and not a["reasons"]]
        selected = None
        if len(ids) == 1:
            authority = ("EXACT_SINGLE_EVENT_PARENT" if prior["resolution_state"] == "EXACT_PARENT" and prior["resolved_parent_id"] == ids[0]
                         else "UNAMBIGUOUS_SINGLE_EVENT_PARENT")
            ambiguity = "UNAMBIGUOUS_PARENT"
        elif len(ids) > 1 and len(proven) == 1:
            authority, ambiguity = "COHERENT_CLUSTER_PARENT", "UNAMBIGUOUS_PARENT"
            selected = proven[0]
        elif ids:
            covered = set().union(*(set(a["member_sweep_ids"]) for a in assessments if not a["reasons"]))
            authority = "AMBIGUOUS_CLUSTER_PARENTS" if set(ids) <= covered else "AMBIGUOUS_SINGLE_EVENT_PARENTS"
            ambiguity = "AMBIGUOUS_PARENT"
        else:
            authority, ambiguity = "NO_VALID_PARENT", "NO_VALID_PARENT"
        membership = ("COHERENT" if len(proven) == 1 else "REACTION_DIVERGENCE" if any("REACTION_DIVERGENCE" in a["reasons"] for a in assessments)
                      else "UNRESOLVED" if assessments else "UNAVAILABLE")
        cid = selected["cluster_id"] if selected else None
        member_ids = selected["member_sweep_ids"] if selected else ids
        parents[did] = {
            "hybrid_parent_id": identity("HYBRID", graph["episode_id"], did, authority, ids, cid, member_ids),
            "symbol": d["symbol"], "displacement_event_id": did, "authority_type": authority,
            "single_parent_event_id": ids[0] if len(ids) == 1 else None,
            "cluster_parent_id": cid, "cluster_view_id": selected["cluster_view_id"] if selected else None,
            "member_parent_event_ids": sorted(member_ids), "eligible_single_parent_ids": ids,
            "parent_ambiguity_state": ambiguity, "cluster_membership_state": membership,
            "relationship_evidence": {"eligible_parent_units": plausible, "excluded_parents": rejected, "cluster_assessments": assessments},
            "available_at": d["available_at"], "origin_checkpoint": checkpoint,
            "provenance_version": VERSION, "research_only": True}
    structures = []
    for sid, e in sorted(es.items()):
        if e["kind"] not in {"BOS", "MSS"}:
            continue
        edges = structure_parents[sid]
        dids = sorted({r["parent"] for r in edges})
        unique = len(dids) == 1 and dids[0] in parents and parents[dids[0]]["authority_type"] in AUTHORITATIVE
        candidate = any(d in parents and parents[d]["eligible_single_parent_ids"] for d in dids)
        structures.append({"structure_event_id": sid, "kind": e["kind"],
                           "classification": "CONTEXTUAL_" + e["kind"] if unique else "AMBIGUOUS_CONTEXTUAL_" + e["kind"] if candidate else "GENERIC_" + e["kind"],
                           "displacement_ids": dids, "hybrid_parent_ids": [parents[d]["hybrid_parent_id"] for d in dids if d in parents],
                           "evidence_relation_ids": sorted(r["relation_id"] for r in edges), "available_at": e["available_at"]})
    chains = []
    for chain in graph["chains"]:
        event_ids = set(chain["event_ids"])
        branches, eligible = [], []
        for did in sorted(event_ids & parents.keys()):
            p = parents[did]
            units = [u for u in p["relationship_evidence"]["eligible_parent_units"] if u["sweep_id"] in event_ids]
            if not units:
                continue
            eligible.append(did)
            kind = "MSS" if chain["chain_type"] == "REVERSAL" else "BOS"
            consequences = [r for r in children[did] if r["type"] == "STRUCTURE_CONSEQUENCE_CANDIDATE" and es[r["child"]]["kind"] == kind]
            zones = [r for r in children[did] if r["type"] in {"FVG_CREATED_BY", "OB_ASSOCIATED_WITH"}]
            reaction = any(r.get("reaction_id") for u in units for r in u["routes"] if not r["contradictions"])
            if consequences and zones and (kind != "MSS" or reaction):
                branches.append({"displacement_id": did, "hybrid_parent_id": p["hybrid_parent_id"],
                                 "member_parent_event_ids": p["member_parent_event_ids"],
                                 "unique_parent": p["authority_type"] in AUTHORITATIVE,
                                 "unique_structure": all(len({x["parent"] for x in structure_parents[r["child"]]}) == 1 for r in consequences),
                                 "structure_event_ids": sorted(r["child"] for r in consequences),
                                 "descendant_zone_ids": sorted(r["child"] for r in zones),
                                 "relation_ids": sorted(r["relation_id"] for r in consequences + zones)})
        state = "PARTIAL" if not branches else "UNAMBIGUOUS" if len(branches) == len(eligible) == 1 and branches[0]["unique_parent"] and branches[0]["unique_structure"] else "AMBIGUOUS"
        chains.append({"hybrid_chain_id": identity("HCHAIN", chain["candidate_chain_id"], branches, state),
                       "source_candidate_chain_id": chain["candidate_chain_id"], "chain_type": chain["chain_type"],
                       "state": state, "branches": branches, "candidate_only": True})
    return {"schema": VERSION, "episode_id": graph["episode_id"], "origin_checkpoint": checkpoint,
            "parents": parents, "structure_consequences": structures, "chains": chains, "outcome_access": False}
