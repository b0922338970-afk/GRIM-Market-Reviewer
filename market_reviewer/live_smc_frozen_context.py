"""Origin-only research capture. No detectors, outcomes, clock, IO or backfill."""
from copy import deepcopy

from .smc_live_sample_status import _frozen_live

SCHEMA = "live-smc-frozen-context.v1"
UNAVAILABLE = "UNAVAILABLE"
STATE_FIELDS = {
    "hybrid_parent_authority": "parent_authority",
    "liquidity_interaction_state": "liquidity_context",
    "liquidity_reaction": "reaction",
    "contextual_mss": "MSS", "contextual_bos": "BOS",
    "chain_linked_fvg": "FVG", "chain_linked_bpr": "BPR",
    "chain_linked_ob": "OB", "confirmed_breaker": "BREAKER",
}
CONTEXT_FIELDS = {
    "phase": "phase", "regime": "regime", "htf_context": "HTF",
    "location": "location", "extension": "extension",
    "liquidity_type": "liquidity_type", "displacement": "displacement",
}


def capture_live_smc_context(record, review, opportunity, latest_closed_m5):
    """Called only after an origin is created; input failures never veto genesis.

    Hybrid fields must be supplied as an existing decision-time smc_state.
    Production labels are not evidence of research Hybrid/BPR/Breaker ancestry.
    """
    stamp = record["origin_snapshot_timestamp"]
    checkpoint = stamp + 300
    payload = {
        "schema": SCHEMA, "origin_id": record["tracker_id"],
        "observation_number": record["origin_observation"],
        "origin_timestamp": stamp, "frozen_at": checkpoint, "available_at": checkpoint,
        "symbol": record["symbol"], "direction": record["direction"],
        "provenance_version": SCHEMA, "source_provenance_version": UNAVAILABLE,
        "single_parent_state": UNAVAILABLE, "cluster_parent_state": UNAVAILABLE,
        "classification_status": "LIVE_NOT_CLASSIFIABLE", "missing_fields": [],
        "capture_reason": None, "smc_state": None,
    }
    payload.update({k: UNAVAILABLE for k in (*STATE_FIELDS, *CONTEXT_FIELDS)})
    aligned = latest_closed_m5 == stamp and opportunity.get("snapshot_timestamp") == stamp
    if aligned:
        structure = review.get("Structure_State") or {}
        payload["phase"] = review.get("Current_Phase") or UNAVAILABLE
        payload["regime"] = review.get("Market_Regime") or UNAVAILABLE
        if isinstance(structure, dict) and structure.get("D1") and structure.get("H4"):
            payload["htf_context"] = [structure["D1"], structure["H4"]]
    try:
        if not aligned:
            raise ValueError("ORIGIN_MARKET_BOUNDARY_MISMATCH")
        state = opportunity.get("smc_state")
        if not isinstance(state, dict):
            raise ValueError("FROZEN_HYBRID_SOURCE_UNAVAILABLE")
        # Validate a projected origin snapshot without exposing outcomes to the reader.
        probe = {k: record[k] for k in ("tracker_id", "symbol", "direction", "origin_snapshot_timestamp", "origin_observation")}
        probe["snapshots"] = [{"snapshot_timestamp": stamp,
                               "observation_number": record["origin_observation"],
                               "available_at": checkpoint, "smc_state": state}]
        state = _frozen_live(probe)
        fields, context = state["fields"], state["matching_context"]
        for dest, source in STATE_FIELDS.items():
            payload[dest] = deepcopy(fields.get(source) or UNAVAILABLE)
        for dest, source in CONTEXT_FIELDS.items():
            payload[dest] = deepcopy(context[source])
        authority = fields["parent_authority"]
        payload["single_parent_state"] = authority if "SINGLE" in authority else "NOT_APPLICABLE"
        payload["cluster_parent_state"] = authority if "CLUSTER" in authority else "NOT_APPLICABLE"
        payload["source_provenance_version"] = state["schema"]
        payload["smc_state"] = deepcopy(state)
    except (ValueError, TypeError, KeyError) as exc:
        payload["capture_reason"] = str(exc)
    required = [*STATE_FIELDS, *CONTEXT_FIELDS, "single_parent_state", "cluster_parent_state", "source_provenance_version"]
    payload["missing_fields"] = sorted(k for k in required if payload[k] in (None, "", "UNKNOWN", UNAVAILABLE))
    if payload["smc_state"] is not None and not payload["missing_fields"]:
        payload["classification_status"] = "CLASSIFIABLE"
    return payload
