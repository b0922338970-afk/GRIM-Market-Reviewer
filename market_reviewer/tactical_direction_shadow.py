"""Forward evidence only: independent MTF shadow labels, never origin eligibility."""
from __future__ import annotations

import copy
import json
import re
from collections import Counter
from pathlib import Path

from .model import TIMEFRAME_SECONDS
from .research_side_diagnostics import DEFAULT_JOURNAL, _candidate_rows
from .smc_live_sample_status import DEFAULT_LIVE_STORE

CONTRACT = {
    "schema": "mtf-direction-shadow-contract.v1", "activation_status": "SHADOW_ONLY",
    "roles": {"MACRO_PRIMARY_DIRECTION": ["D1", "H4"], "SWING_SETUP_CONTEXT": ["H4", "H1"],
              "TACTICAL_DIRECTION": ["H1", "M15"], "EXECUTION_TRIGGER": ["M15", "M5"]},
    "primary_source": "D1_H4_SWING_BIAS", "origin_creation_allowed": False,
    "context_rule": "Latest visible H1 MSS/BOS plus agreeing M15 structure or non-invalidated tactical FVG/OB; no recency ranking implies causal linkage",
    "confirmation_rule": "M15 alignment plus exposed same-chain liquidity/displacement/MSS/setup and existing eligible-retest identity contract; shadow label is not ARMED or execution permission",
    "future_fields": {"origin_scope": ["PRIMARY", "TACTICAL"],
                      "direction_source": ["PRIMARY_SWING_BIAS", "TACTICAL_H1_M15"],
                      "counter_trend_to_primary": [True, False], "enabled": False},
}
SIDES = {"BULLISH": "LONG", "BEARISH": "SHORT"}


def _visible(stamp, tf, checkpoint):
    return type(stamp) is int and stamp > 0 and tf in TIMEFRAME_SECONDS and stamp + TIMEFRAME_SECONDS[tf] <= checkpoint


def _breaks(review, tf, checkpoint):
    result = []
    for name in ("Last_MSS", "Last_BOS"):
        value = (review.get(name) or {}).get(tf) if isinstance(review.get(name), dict) else None
        match = re.fullmatch(r"(BULLISH|BEARISH) ([0-9.]+) @ (\d+)", value or "")
        if match and _visible(int(match[3]), tf, checkpoint):
            result.append({"type": name, "direction": match[1], "timestamp": int(match[3]), "timeframe": tf, "reference": value})
    return result


def relationship(primary, tactical):
    if tactical == "UNAVAILABLE":
        return "UNAVAILABLE"
    if "NONE" in (primary, tactical):
        return "NEUTRAL"
    return "ALIGNED" if primary == tactical else "COUNTER_TREND"


def classify_tactical(review, checkpoint, observation=None, symbol=None):
    """Classify only exported closed evidence; never consult outcomes or later rows."""
    primary = SIDES.get(review.get("Swing_Bias"), "NONE")
    h1 = _breaks(review, "H1", checkpoint)
    m15 = _breaks(review, "M15", checkpoint)
    latest_h1 = [e for e in h1 if e["timestamp"] == max(x["timestamp"] for x in h1)] if h1 else []
    biases = {e["direction"] for e in latest_h1}
    bias = next(iter(biases)) if len(biases) == 1 else None
    supporting, missing = [], []
    inventory = []
    for field in ("FVG", "Order_Blocks"):
        for zone in review.get(field, []) if isinstance(review.get(field), list) else []:
            if isinstance(zone, dict) and zone.get("timeframe") in {"H1", "M15", "M5"} and _visible(zone.get("formed_at"), zone.get("timeframe"), checkpoint):
                inventory.append(dict(zone, source_field=field))
    zones = [z for z in inventory if z.get("direction") == bias and z.get("status") in {"FRESH", "TOUCHED", "MITIGATED"}]
    m15_latest = [e for e in m15 if e["timestamp"] == max(x["timestamp"] for x in m15)] if m15 else []
    m15_aligned = bool(m15_latest) and {e["direction"] for e in m15_latest} == {bias}
    exposed = bool(h1 or m15 or inventory) or any(isinstance(review.get(k), dict) for k in ("Last_MSS", "Last_BOS"))
    tactical, state = ("NONE", "TACTICAL_NONE") if exposed else ("UNAVAILABLE", "UNAVAILABLE")
    if bias:
        supporting.extend(latest_h1)
        state = "TACTICAL_CONTEXT"
        if m15_aligned or zones:
            tactical, state = SIDES[bias], "TACTICAL_INCOMPLETE"
            supporting.extend(m15_latest if m15_aligned else [])
            supporting.extend({"type": z["source_field"], "timeframe": z["timeframe"],
                               "formed_at": z["formed_at"], "status": z.get("status"),
                               "direction": z["direction"]} for z in zones)
        else:
            missing.append("INDEPENDENT_TACTICAL_CORROBORATION")
    else:
        missing.append("UNAMBIGUOUS_H1_STRUCTURE")
    if not m15_aligned:
        missing.append("M15_DIRECTIONAL_CONFIRMATION")

    displacement = re.match(r"(BULLISH|BEARISH) (VALID|STRONG) @ (\d+);", str(review.get("Displacement", "")))
    disp_ok = bool(displacement and displacement[1] == bias and int(displacement[3]) < checkpoint
                   and h1 and int(displacement[3]) >= max(e["timestamp"] for e in latest_h1))
    if not disp_ok:
        missing.append("DIRECTIONAL_DISPLACEMENT")
    contextual = re.fullmatch(r"(BULLISH|BEARISH) @ (\d+); related_sweep_id=(\w+):(\d+); related_displacement_id=(BULLISH|BEARISH):(\d+)", str(review.get("Contextual_MSS", "")))
    trigger_mss = [e for tf in ("M15", "M5") for e in _breaks(review, tf, checkpoint) if e["type"] == "Last_MSS"]
    linked = bool(disp_ok and contextual and contextual[1] == bias and contextual[5] == bias
                  and int(contextual[6]) == int(displacement[3]) and int(contextual[4]) < int(displacement[3]) <= int(contextual[2])
                  and any(e["timestamp"] == int(contextual[2]) and e["direction"] == bias for e in trigger_mss))
    if not linked:
        missing.append("LINKED_CONTEXTUAL_TRIGGER")
    events = [e for e in review.get("Liquidity_Events", []) if isinstance(e, dict)
              and _visible(e.get("timestamp"), e.get("timeframe"), checkpoint)] if isinstance(review.get("Liquidity_Events"), list) else []
    side = "Sell-side" if bias == "BULLISH" else "Buy-side"
    swept = [e for e in events if linked and e.get("event_type") == "SWEPT"
             and e.get("timeframe") == contextual[3] and e.get("timestamp") == int(contextual[4])
             and (side in str(e.get("level_type")) or e.get("level_type") == ("Equal Lows" if bias == "BULLISH" else "Equal Highs"))]
    reclaimed = [e for e in events if e.get("event_type") == "RECLAIMED" and any(
        e.get("level_price") == s.get("level_price") and e.get("level_type") == s.get("level_type")
        and e.get("timeframe") == s.get("timeframe") and s["timestamp"] <= e["timestamp"] <= int(displacement[3]) for s in swept)]
    if not reclaimed:
        missing.append("LINKED_LIQUIDITY_CONFIRMATION")
    active_id = review.get("Active_Setup_ID")
    setups = [z for z in zones if z["source_field"] == "FVG" and linked and z.get("setup_type") == "SETUP_FVG"
              and z.get("related_displacement_id") == f"{bias}:{displacement[3]}"
              and z["formed_at"] >= int(contextual[2])
              and f"{z['timeframe']}-{bias}-SETUP_FVG-{z['formed_at']}" == active_id]
    if not setups:
        missing.append("LINKED_ACTIVE_DEFENDABLE_ZONE")
    try:
        retest_time = int(review.get("Eligible_Retest_Timestamp", 0))
    except (TypeError, ValueError):
        retest_time = 0
    retest = bool(setups and review.get("Eligible_Retest_Confirmed") == "YES"
        and review.get("Eligible_Retest_Setup_ID") == active_id
        and review.get("Eligible_Retest_Evidence_ID") not in (None, "NONE", "")
        and retest_time > setups[0]["formed_at"] and _visible(retest_time, setups[0]["timeframe"], checkpoint))
    if not retest:
        missing.append("ELIGIBLE_EXECUTION_TRIGGER")
    if disp_ok:
        supporting.append({"type": "EXPOSED_DISPLACEMENT", "value": review.get("Displacement"),
                           "timeframe": "NOT_EXPOSED", "provenance": "CLOSED_REVIEW_OUTPUT"})
    if linked:
        supporting.append({"type": "LINKED_CONTEXTUAL_MSS", "value": review.get("Contextual_MSS")})
    if reclaimed:
        supporting.extend({"type": "LINKED_LIQUIDITY_RECLAIM", "event": copy.deepcopy(e)} for e in reclaimed)
    if retest:
        supporting.append({"type": "ELIGIBLE_RETEST", "setup_id": active_id, "timestamp": retest_time})
    if tactical in {"LONG", "SHORT"} and not missing:
        state = "TACTICAL_CONFIRMED"
    context = review.get("smc_context") if isinstance(review.get("smc_context"), dict) else {}
    return {"schema": "tactical-direction-shadow.v1", "activation_status": "SHADOW_ONLY",
        "observation": observation, "symbol": symbol or review.get("Symbol"), "checkpoint": checkpoint,
        "primary_direction": primary, "primary_source": "D1_H4_SWING_BIAS",
        "tactical_direction": tactical, "relationship": relationship(primary, tactical), "state": state,
        "execution_trigger": "CONFIRMED" if state == "TACTICAL_CONFIRMED" else "UNAVAILABLE_OR_INCOMPLETE",
        "supporting_evidence": supporting, "missing_evidence": sorted(set(missing)),
        "exposed_context": {"H1_protected_high": (review.get("Protected_High") or {}).get("H1", "UNAVAILABLE"),
            "H1_protected_low": (review.get("Protected_Low") or {}).get("H1", "UNAVAILABLE"),
            "global_phase": review.get("Current_Phase", "UNAVAILABLE"), "M15_phase": "UNAVAILABLE",
            "draw": review.get("Tactical_Draw_on_Liquidity", "UNAVAILABLE"),
            "bpr": copy.deepcopy(context.get("bpr_state", "UNAVAILABLE")),
            "breaker": copy.deepcopy(context.get("breaker_state", "UNAVAILABLE")),
            "displacement": review.get("Displacement", "UNAVAILABLE")},
        "origin_creation_allowed": False}


def tactical_direction_shadow(journal_path=DEFAULT_JOURNAL, live_store=DEFAULT_LIVE_STORE):
    result = {"schema": "tactical-direction-shadow-report.v1", "contract": copy.deepcopy(CONTRACT),
              "read_only": True, "view": "CORRECTED_SHADOW_VIEW", "segments": {},
              "recommendation": "KEEP_SHADOW_ONLY"}
    try:
        paths = [Path(journal_path), Path(live_store)]
        before = [p.read_bytes() for p in paths]
        journal, store = [json.loads(b) for b in before]
        if store.get("schema") != "missed-opportunity-tracker.v1":
            raise ValueError("INVALID_STORE")
        records = [r for r in store["records"] if r.get("sample_source") != "HISTORICAL_REPLAY"
                   and not str(r.get("tracker_id", "")).startswith("HIST-")]
        rows, conflicts = _candidate_rows(journal, records)
        if not rows or conflicts:
            raise ValueError("INVALID_ARCHIVE")
        reviews = {}
        exposures = {}
        for tx in journal.get("transactions", []):
            if tx.get("status") != "COMPLETE" or tx.get("sample_source") == "HISTORICAL_REPLAY":
                continue
            for symbol, review in (tx.get("recovery_payload", {}).get("reviews") or {}).items():
                key = (tx.get("observation_number"), symbol)
                if key in reviews and reviews[key] != review:
                    raise ValueError("CONFLICTING_REVIEW")
                reviews[key] = review
                exposure = (tx.get("recovery_payload", {}).get("non_canonical_research_evidence") or {}).get("tactical_provenance", {}).get(symbol)
                if key in exposures and exposures[key] != exposure:
                    raise ValueError("CONFLICTING_EXPOSURE")
                exposures[key] = exposure
        classified = []
        for row in rows:
            review = reviews.get((row["observation"], row["symbol"])) if row["source"] == "COMPLETE_JOURNAL" else None
            if review is None:
                review = {"Swing_Bias": (row.get("evidence") or {}).get("SWING_BIAS")}
            item = classify_tactical(review, row["timestamp"] + 300, row["observation"], row["symbol"])
            from .tactical_provenance import bind_tactical_identity
            binding = bind_tactical_identity(review, exposures.get((row["observation"], row["symbol"])), row["timestamp"] + 300)
            item["tactical_provenance_binding"] = binding
            if binding["status"] == "BOUND":
                item["state"] = "TACTICAL_CONFIRMED"
                item["execution_trigger"] = "CONFIRMED"
                item["missing_evidence"] = []
            classified.append(item)
        if before != [p.read_bytes() for p in paths]:
            raise ValueError("SOURCE_CHANGED")
        end = max(r["checkpoint"] for r in classified)
        start = min(r["checkpoint"] for r in classified)
        result["window"] = {"first_observation": min(r["observation"] for r in classified),
                            "last_observation": max(r["observation"] for r in classified), "checkpoint": end}
        for name, cutoff in (("latest_24H", end-86400), ("latest_72H", end-259200), ("full_window", start)):
            result["segments"][name] = {}
            for symbol in ("BTC", "ETH"):
                cohort = [r for r in classified if r["symbol"] == symbol and cutoff <= r["checkpoint"] <= end]
                counts = {"PRIMARY_"+d: sum(r["primary_direction"] == d for r in cohort) for d in ("LONG", "SHORT", "NONE")}
                for d in ("LONG", "SHORT"):
                    counts["TACTICAL_"+d+"_CONTEXT"] = sum(r["tactical_direction"] == d and r["state"] != "TACTICAL_CONFIRMED" for r in cohort)
                    counts["TACTICAL_"+d+"_CONFIRMED"] = sum(r["tactical_direction"] == d and r["state"] == "TACTICAL_CONFIRMED" for r in cohort)
                    counts["COUNTER_TREND_"+d] = sum(r["tactical_direction"] == d and r["relationship"] == "COUNTER_TREND" for r in cohort)
                    counts["TACTICAL_"+d+"_INCOMPLETE"] = sum(r["tactical_direction"] == d and r["state"] == "TACTICAL_INCOMPLETE" for r in cohort)
                    counts["COUNTER_TREND_"+d+"_CONFIRMED"] = sum(r["tactical_direction"] == d and r["relationship"] == "COUNTER_TREND" and r["state"] == "TACTICAL_CONFIRMED" for r in cohort)
                    counts["COUNTER_TREND_"+d+"_INCOMPLETE"] = sum(r["tactical_direction"] == d and r["relationship"] == "COUNTER_TREND" and r["state"] == "TACTICAL_INCOMPLETE" for r in cohort)
                counts.update({state: sum(r["state"] == state for r in cohort) for state in
                               ("TACTICAL_NONE", "TACTICAL_CONTEXT", "TACTICAL_INCOMPLETE", "UNAVAILABLE")})
                counts["missing_evidence"] = dict(sorted(Counter(m for r in cohort if r["state"] == "TACTICAL_INCOMPLETE" for m in r["missing_evidence"]).items()))
                result["segments"][name][symbol] = counts
        result["representative_cases"] = {}
        for primary, tactical in (("LONG", "SHORT"), ("NONE", "SHORT"), ("LONG", "LONG")):
            matches = [r for r in classified if r["primary_direction"] == primary and r["tactical_direction"] == tactical]
            result["representative_cases"][primary+"_"+tactical] = matches[-2:]
        result["status"] = "PASS"
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        result["segments"] = {}
        result["status"] = "UNAVAILABLE"
    return result


def render_shadow(result):
    return "\n".join(["TIMEFRAME CONTRACT", json.dumps(result["contract"], sort_keys=True),
        "PRIMARY DISTRIBUTION / TACTICAL DISTRIBUTION / COUNTER-TREND CONTEXT / CONFIRMED / INCOMPLETE",
        json.dumps(result["segments"], indent=2, sort_keys=True), "REPRESENTATIVE CASES",
        json.dumps(result.get("representative_cases", {}), indent=2, sort_keys=True), result["recommendation"]])
