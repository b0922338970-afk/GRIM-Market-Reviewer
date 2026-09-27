"""Output-only SMC explanation. Neither packets nor research zones are gates."""
from copy import deepcopy

from .live_hybrid_smc import build_live_hybrid_smc, legal_prefix
from .opportunity import extract_opportunity_snapshot


def evidence(value=None, reason="EVIDENCE_NOT_EXPOSED"):
    unavailable = value is None or value == "" or (isinstance(value, str) and value in {"UNKNOWN", "UNAVAILABLE", "N/A", "NOT_REPORTED"})
    return {"availability": "UNAVAILABLE" if unavailable else "AVAILABLE",
            "value": None if unavailable else deepcopy(value),
            "reason": reason if unavailable else None}


def enrich_review(review, frames):
    """Run once after native replay; leave every pre-existing review field intact."""
    result = deepcopy(review)
    names = ("active_draw dealing_range premium_discount liquidity_type liquidity_reaction "
             "displacement_quality contextual_mss contextual_bos fvg_state bpr_state ob_state "
             "breaker_state irl erl remaining_opportunity defense_state fragility_flags").split()
    context = {name: evidence() for name in names}
    context.update(schema="production-smc-context.v1", policy_impact="NONE",
                   available_at=None, provenance=None)
    opportunity = {}
    try:
        stamp = int(review["Review_Timestamp"])
        checkpoint = stamp + 300
        context["available_at"] = checkpoint
        prefix = legal_prefix(frames, review["Symbol"], checkpoint)
        opportunity = extract_opportunity_snapshot(review, prefix)
        record = {"tracker_id": f"SMC-OUTPUT-{review['Symbol']}-{stamp}",
                  "origin_snapshot_timestamp": stamp, "symbol": review["Symbol"],
                  "direction": review.get("Preferred_Direction")}
        payload = build_live_hybrid_smc(record, review, opportunity, prefix)
        context["producer_status"] = payload["status"]
        context["producer_reason"] = payload["reason"]
        state = payload.get("smc_state") or {}
        fields = state.get("fields", {})
        matching = state.get("matching_context", {})
        context["provenance"] = state.get("producer_provenance")
        for name, source in {"premium_discount": "location", "liquidity_reaction": "reaction",
                             "contextual_mss": "MSS", "contextual_bos": "BOS",
                             "fvg_state": "FVG", "bpr_state": "BPR", "ob_state": "OB",
                             "breaker_state": "BREAKER"}.items():
            context[name] = evidence(fields.get(source))
        context["liquidity_type"] = evidence(matching.get("liquidity_type"))
        context["displacement_quality"] = evidence(matching.get("displacement"))
        context["remaining_opportunity"] = evidence({k: evidence(v) for k, v in
            state.get("remaining_room_metrics", {}).items()} or None)
        context["fragility_flags"] = evidence(opportunity.get("risk_signatures"))
        context["active_draw"] = evidence({
            "htf": evidence(review.get("Macro_Draw_on_Liquidity") if
                            "NONE" not in str(review.get("Macro_Draw_on_Liquidity")) else None),
            "tactical": evidence(review.get("Active_Tactical_Draw") if
                                 review.get("Active_Tactical_Draw") != "NONE" else None)})
        # Only explicit detector classifications are exposed; no inferred IRL/ERL zones.
        for key, label in (("irl", "INTERNAL"), ("erl", "EXTERNAL")):
            levels = review.get("Liquidity", [])
            if isinstance(levels, list):
                selected = [v for v in levels if isinstance(v, dict) and
                            str(v.get("type", "")).upper().startswith(label + " ")]
                context[key] = evidence(selected or None)
        context["defense_state"] = evidence({k: v for k, v in state.get("selected_zones", {}).items()
                                             if v.get("observations")} or None,
                                              "NO_SELECTED_ZONE_DEFENSE_EVIDENCE")
    except Exception as exc:
        # Explanation availability must never veto the already finalized decision.
        context["producer_status"] = "UNAVAILABLE"
        context["producer_reason"] = type(exc).__name__ + ":" + str(exc)
    truth = opportunity.get("truth", {})
    state = review.get("State")
    level = {"WATCH": "NORMAL_ALERT", "ARMED": "HIGH_PRIORITY_ALERT"}.get(state, "SILENT")
    packet = {
        "schema": "opportunity_alert.v1", "symbol": review.get("Symbol"),
        "timeframe": "M5", "direction": review.get("Preferred_Direction"),
        "review_state": state, "alert_level": level,
        "emit_alert": level != "SILENT", "execution_ready": state == "ARMED",
        "quality_context": deepcopy(context),
        "market_story": f"{review.get('Symbol')} | {review.get('Swing_Bias')} | "
                        f"{review.get('Current_Phase')} | {review.get('Sequence_State')} | {state}; "
                        + "; ".join(f"{k}={context[k]['value'] if context[k]['availability'] == 'AVAILABLE' else 'UNAVAILABLE'}"
                                    for k in ('liquidity_reaction', 'displacement_quality', 'contextual_mss',
                                              'contextual_bos', 'fvg_state', 'bpr_state', 'ob_state', 'breaker_state')),
        "entry_zone": evidence(truth.get("setup_poi")),
        "invalidation": evidence({k: v for k, v in review.get("Structural_Invalidation", {}).items()
                                   if v not in (None, "NONE", "UNAVAILABLE")} or None),
        "target": evidence(review.get("Macro_Draw_on_Liquidity") if
                           "NONE" not in str(review.get("Macro_Draw_on_Liquidity")) else None),
        "remaining_room": deepcopy(context["remaining_opportunity"]),
        "fragility": deepcopy(context["fragility_flags"]),
        "generated_at": context["available_at"], "delivery": "PACKET_ONLY",
    }
    result.update(smc_context=context, opportunity_alert=packet)
    return result
