"""Read-only attribution of exposed evidence gaps, not a new confirmation rule."""
from __future__ import annotations

import copy
import json
import re
from collections import Counter
from pathlib import Path

from .research_side_diagnostics import DEFAULT_JOURNAL, _candidate_rows
from .smc_live_sample_status import DEFAULT_LIVE_STORE
from .tactical_direction_shadow import _breaks, _visible, classify_tactical

STATES = ("AVAILABLE_AND_BOUND", "AVAILABLE_NOT_BOUND", "COMPUTED_NOT_EXPOSED",
          "NOT_COMPUTED", "SOURCE_UNAVAILABLE", "CONTRACT_MISSING", "NOT_APPLICABLE", "UNKNOWN")
CAPABILITIES = {
    "displacement": {
        "source": "market_reviewer/reviewer.py:find_displacements / _latest_displacement / review_symbol",
        "raw_detector_directions": ["BULLISH", "BEARISH"],
        "producer_bias_filtered": False, "SELECTED_DISPLACEMENT_BIAS_FILTERED": True,
        "raw_list_exposure": "COMPUTED_NOT_EXPOSED", "raw_occurrence_per_case": "UNKNOWN",
        "selection": "After selected active sweep, matching Swing Bias, VALID/STRONG, latest timestamp",
        "timeframe": "Per-TF detector map is flattened; selected text has no timeframe",
        "safe_future_exposure": "Possible research-only adapter; retain TF and confirmation availability. Not implemented by audit.",
        "availability_caveat": "Detector follow_through inspects up to two later closed candles; event open time alone is not availability.",
    },
    "m15": {
        "source": "market_reviewer/reviewer.py:analyze_structure / _current_phase / _contextual_mss",
        "M15_phase": "NOT_COMPUTED", "structure": "Last_BOS/Last_MSS.M15 carry direction/open timestamp",
        "confirmation": "Existing V4.7.4 uses latest M15 structure alignment, not a new producer",
        "contextual_trigger": "Primary-bias selected M15/M5 MSS; text omits trigger TF",
        "independent_tactical_setup_contract": "CONTRACT_MISSING",
    },
    "liquidity": {
        "source": "market_reviewer/reviewer.py:find_liquidity_events / _contextual_mss_text",
        "producer": "Sweep/reclaim with level_price, level_type, timeframe, timestamp",
        "exposure": "Last 24 cross-TF events only; missing row does not prove absence",
        "identity": "Contextual reference TF:timestamp omits level identity; displacement direction:timestamp omits TF",
    },
    "setup": {
        "source": "market_reviewer/reviewer.py:_setup_id / _eligible_setup_retest; production_smc_context.py",
        "production_identity": "Active_Setup_ID and Eligible_Retest_Setup_ID exist for locked production FVG",
        "inventory": "FVG last 24; OB last 12; inventory is not tactical ancestry",
        "independent_tactical_identity": "CONTRACT_MISSING",
        "bpr_breaker": "SMC summary wrappers do not establish complete tactical event identity",
    },
}


def _entry(value, path, timeframe, timestamp, state, reason, shadow_pass=False):
    return {"value": copy.deepcopy(value), "source_module": "market_reviewer/reviewer.py",
            "source_timeframe": timeframe, "timestamp": timestamp, "exposed_path": path,
            "binding_status": state, "missing_reason": reason, "existing_shadow_requirement_pass": shadow_pass}


def audit_review(review, checkpoint, observation=None, symbol=None):
    shadow = classify_tactical(review, checkpoint, observation, symbol)
    missing = shadow["missing_evidence"]
    bias = {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(shadow["tactical_direction"])
    disp = review.get("Displacement")
    match = re.match(r"(BULLISH|BEARISH) (VALID|STRONG|WEAK) @ (\d+);", str(disp))
    h1 = _breaks(review, "H1", checkpoint)
    if not match:
        disp_reason = "SELECTED_NONE_RAW_UNKNOWN" if disp == "NONE" else "SELECTED_UNAVAILABLE_OR_UNPARSEABLE"
    elif match[1] != bias:
        disp_reason = "SELECTED_OPPOSITE_DIRECTION"
    elif match[2] == "WEAK":
        disp_reason = "SELECTED_STRENGTH_REJECTED"
    elif int(match[3]) >= checkpoint:
        disp_reason = "SELECTED_TIMESTAMP_NOT_BEFORE_CHECKPOINT"
    elif h1 and int(match[3]) < max(e["timestamp"] for e in h1):
        disp_reason = "SELECTED_PRECEDES_H1_STRUCTURE_SHADOW_ORDER"
    else:
        disp_reason = "SELECTED_PASSES_SHADOW_BUT_TIMEFRAME_NOT_EXPOSED"
    m15 = _breaks(review, "M15", checkpoint)
    latest = [e for e in m15 if e["timestamp"] == max(x["timestamp"] for x in m15)] if m15 else []
    m15_reason = ("NO_VISIBLE_SELECTED_M15_STRUCTURE_RAW_UNKNOWN" if not latest else
                  "M15_DIRECTION_CONFLICT" if len({e["direction"] for e in latest}) > 1 else
                  "M15_OPPOSITE_DIRECTION" if latest[0]["direction"] != bias else "M15_ALIGNED_NOT_SETUP_LINKAGE")
    liquidity = [e for e in review.get("Liquidity_Events", []) if isinstance(e, dict)
                 and _visible(e.get("timestamp"), e.get("timeframe"), checkpoint)]
    zones = {k: [z for z in review.get(k, []) if isinstance(z, dict)
                 and _visible(z.get("formed_at"), z.get("timeframe"), checkpoint)] for k in ("FVG", "Order_Blocks")}
    context = review.get("smc_context") or {}
    contextual = re.match(r"(BULLISH|BEARISH) @ (\d+);", str(review.get("Contextual_MSS", "")))
    entries = {
        "directional_displacement": _entry(disp, "Displacement", "NOT_EXPOSED",
            int(match[3]) if match else None, "AVAILABLE_NOT_BOUND" if match else "UNKNOWN", disp_reason,
            "DIRECTIONAL_DISPLACEMENT" not in missing),
        "M15_confirmation": _entry(latest, "Last_MSS.M15 / Last_BOS.M15", "M15",
            latest[0]["timestamp"] if latest else None,
            "AVAILABLE_AND_BOUND" if "M15_DIRECTIONAL_CONFIRMATION" not in missing else
            "AVAILABLE_NOT_BOUND" if latest else "UNKNOWN", m15_reason,
            "M15_DIRECTIONAL_CONFIRMATION" not in missing),
        "M15_phase": _entry("UNAVAILABLE", "NOT_EXPOSED", "M15", None, "NOT_COMPUTED", "ONLY_GLOBAL_CURRENT_PHASE"),
        "contextual_trigger": _entry(review.get("Contextual_MSS"), "Contextual_MSS", "NOT_EXPOSED",
            int(contextual[2]) if contextual else None,
            "AVAILABLE_NOT_BOUND" if review.get("Contextual_MSS") not in (None, "NONE", "UNAVAILABLE") else "UNKNOWN",
            "TRIGGER_TF_AND_INDEPENDENT_TACTICAL_SETUP_ID_NOT_EXPOSED", "LINKED_CONTEXTUAL_TRIGGER" not in missing),
        "liquidity": _entry(liquidity, "Liquidity_Events", [e["timeframe"] for e in liquidity],
            [e["timestamp"] for e in liquidity], "AVAILABLE_NOT_BOUND" if liquidity else "UNKNOWN",
            "PRESENCE_NOT_IDENTITY_PROOF_TRUNCATED_INVENTORY", "LINKED_LIQUIDITY_CONFIRMATION" not in missing),
        "setup": _entry({**zones, "active_setup_id": review.get("Active_Setup_ID"),
            "bpr": context.get("bpr_state", "UNAVAILABLE"), "breaker": context.get("breaker_state", "UNAVAILABLE")},
            "FVG / Order_Blocks / Active_Setup_ID / smc_context",
            [z["timeframe"] for values in zones.values() for z in values],
            [z["formed_at"] for values in zones.values() for z in values],
            "AVAILABLE_NOT_BOUND" if any(zones.values()) else "UNKNOWN",
            "TACTICAL_CHAIN_IDENTITY_MISSING", "LINKED_ACTIVE_DEFENDABLE_ZONE" not in missing),
        "retest": _entry({k: review.get(k) for k in ("Eligible_Retest_Confirmed", "Eligible_Retest_Timestamp",
            "Eligible_Retest_Evidence_ID", "Eligible_Retest_Setup_ID")}, "Eligible_Retest_*", "NOT_EXPLICIT_IN_RETEST_ROW",
            review.get("Eligible_Retest_Timestamp"), "AVAILABLE_NOT_BOUND" if "Eligible_Retest_Confirmed" in review else "UNKNOWN",
            "PRODUCTION_SETUP_IDENTITY_NOT_INDEPENDENT_TACTICAL_CHAIN", "ELIGIBLE_EXECUTION_TRIGGER" not in missing),
    }
    return {"observation": observation, "symbol": symbol, "checkpoint": checkpoint,
        "primary_direction": shadow["primary_direction"], "tactical_direction": shadow["tactical_direction"],
        "relationship": shadow["relationship"], "shadow_state": shadow["state"], "missing_evidence": missing,
        "requirements": entries, "RAW_BULLISH_DISPLACEMENT_AVAILABLE": "UNKNOWN",
        "RAW_BEARISH_DISPLACEMENT_AVAILABLE": "UNKNOWN", "DISPLACEMENT_BINDING_GAP": True,
        "conclusion": "NOT_CONFIRMABLE_WITH_EXISTING_EVIDENCE",
        "conclusion_reason": "Selected displacement TF/availability identity not exposed; audit never promotes shadow state",
        "countertrend_readiness_bucket": "E_UNKNOWN",
        "bucket_reason": "Archived reviews omit raw universe and per-event availability; A-D cannot be proven. Zero bucket counts mean no proven assignments, not absence.",
        "origin_creation_allowed": False}


def summarize(traces):
    incomplete = [t for t in traces if t["shadow_state"] == "TACTICAL_INCOMPLETE"]
    def reasons(requirement, missing):
        return dict(sorted(Counter(t["requirements"][requirement]["missing_reason"] for t in incomplete
                                   if missing in t["missing_evidence"]).items()))
    counter = [t for t in traces if t["relationship"] == "COUNTER_TREND" and t["tactical_direction"] == "SHORT"]
    return {"rows": len(traces), "incomplete": len(incomplete),
        "displacement_missing": reasons("directional_displacement", "DIRECTIONAL_DISPLACEMENT"),
        "M15_missing": reasons("M15_confirmation", "M15_DIRECTIONAL_CONFIRMATION"),
        "liquidity_present": sum(bool(t["requirements"]["liquidity"]["value"]) for t in incomplete),
        "existing_shadow_confirmed": sum(t["shadow_state"] == "TACTICAL_CONFIRMED" for t in traces),
        "countertrend_short": {s: {"count": sum(t["symbol"] == s for t in counter),
            "A_BINDING_ONLY": 0, "B_COMPUTED_UNEXPOSED": 0, "C_CAPABILITY_ABSENT": 0,
            "D_GENUINE_INCOMPLETE": 0, "E_UNKNOWN": sum(t["symbol"] == s for t in counter)} for s in ("BTC", "ETH")}}


def tactical_evidence_audit(journal_path=DEFAULT_JOURNAL, live_store=DEFAULT_LIVE_STORE):
    result = {"schema": "tactical-evidence-binding-audit.v1", "read_only": True,
              "capabilities": copy.deepcopy(CAPABILITIES), "availability_states": list(STATES)}
    try:
        paths = [Path(journal_path), Path(live_store)]
        before = [p.read_bytes() for p in paths]
        journal, store = [json.loads(b) for b in before]
        if store.get("schema") != "missed-opportunity-tracker.v1":
            raise ValueError("INVALID_STORE")
        records = [r for r in store["records"] if r.get("sample_source") != "HISTORICAL_REPLAY"
                   and not str(r.get("tracker_id", "")).startswith("HIST-")]
        rows, conflicts = _candidate_rows(journal, records)
        if conflicts or not rows:
            raise ValueError("INVALID_ARCHIVE")
        reviews = {}
        for tx in journal.get("transactions", []):
            if tx.get("status") != "COMPLETE" or tx.get("sample_source") == "HISTORICAL_REPLAY":
                continue
            for symbol, review in (tx.get("recovery_payload", {}).get("reviews") or {}).items():
                key = (tx.get("observation_number"), symbol)
                if key in reviews and reviews[key] != review:
                    raise ValueError("CONFLICTING_REVIEW")
                reviews[key] = review
        traces = [audit_review(reviews.get((r["observation"], r["symbol"]), {})
                    if r["source"] == "COMPLETE_JOURNAL" else {}, r["timestamp"] + 300,
                    r["observation"], r["symbol"]) for r in rows if r["observation"] >= 46]
        baseline = [t for t in traces if t["observation"] <= 276]
        chosen = [t for t in baseline if t["observation"] in (247, 276)]
        for direction in ("SHORT", "LONG"):
            candidates = [t for t in baseline if t["tactical_direction"] == direction]
            if candidates:
                chosen.append(dict(min(candidates, key=lambda t: (len(t["missing_evidence"]), t["observation"], t["symbol"])),
                                   selection="FEWEST_EXISTING_SHADOW_MISSING_THEN_OBSERVATION_SYMBOL"))
        if before != [p.read_bytes() for p in paths]:
            raise ValueError("SOURCE_CHANGED")
        result.update(status="PASS", baseline_46_276=summarize(baseline), full_window=summarize(traces),
            latest_observation=max(t["observation"] for t in traces), representative_traces=chosen,
            traces=traces, assessment="MIXED_EVIDENCE_GAP",
            assessment_evidence=["Raw per-TF list computed but selected-only exposure", "Selected displacement lacks TF",
                "M15 phase not produced", "Independent tactical setup/retest identity contract missing"],
            binding_only_sufficiency="UNKNOWN", market_absence="UNKNOWN", recommendation="KEEP_SHADOW_ONLY")
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        result.update(status="UNAVAILABLE", assessment="INSUFFICIENT_EVIDENCE")
    return result


def render_evidence_audit(result):
    sections = [("DISPLACEMENT", result["capabilities"]["displacement"]),
        ("M15 CONFIRMATION", result["capabilities"]["m15"]), ("LIQUIDITY LINKAGE", result["capabilities"]["liquidity"]),
        ("SETUP/RETEST CHAIN", result["capabilities"]["setup"]), ("COUNTER_TREND READINESS", result.get("baseline_46_276")),
        ("REPRESENTATIVE TRACES", result.get("representative_traces")), ("ASSESSMENT", result["assessment"])]
    return "\n".join(title + "\n" + json.dumps(value, indent=2, sort_keys=True) for title, value in sections)
