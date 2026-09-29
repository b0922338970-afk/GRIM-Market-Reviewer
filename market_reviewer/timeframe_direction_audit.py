"""Read-only routing and exposed-evidence audit. No candidate promotion."""
from __future__ import annotations

import inspect
import json
import re
from collections import Counter
from pathlib import Path

from . import reviewer
from .missed_opportunity_live import _direction_from_review
from .model import TIMEFRAME_SECONDS
from .research_maturity import research_maturity
from .research_side_diagnostics import DEFAULT_JOURNAL, _candidate_rows
from .smc_live_sample_status import DEFAULT_LIVE_STORE, DEFAULT_OUTCOMES, DEFAULT_ROOT

BIAS_BUCKETS = ("BULLISH", "BEARISH", "NONE", "UNAVAILABLE", "OTHER")


def bias_bucket(value):
    if value is None or value in ("", "UNAVAILABLE", "DATA_UNAVAILABLE"):
        return "UNAVAILABLE"
    return value if value in BIAS_BUCKETS else "OTHER"


def routing_contract():
    code, line = inspect.getsourcelines(_direction_from_review)
    review_code, review_line = inspect.getsourcelines(reviewer.review_symbol)
    preferred = [(review_line + i, text.strip()) for i, text in enumerate(review_code) if 'Preferred_Direction=' in text]
    return {
        "flag": "DIRECTION_DEFAULT_ASYMMETRY",
        "research": {"file": "market_reviewer/missed_opportunity_live.py",
            "function": "_direction_from_review", "line": line, "code": ''.join(code).strip(),
            "mapping": {b: _direction_from_review({"Swing_Bias": b}) for b in BIAS_BUCKETS},
            "downstream": "build_tracker_candidate_from_observation -> _apply_symbol_observation -> direction-specific tracker identity; eligibility still required"},
        "reviewer": {"file": "market_reviewer/reviewer.py", "function": "review_symbol",
            "branches": preferred, "downstream": "Preferred_Direction -> opportunity_alert.direction -> website; NONE preserved"},
        "propagation": [
            {"file": "market_reviewer/production_smc_context.py", "function": "enrich_review",
             "behavior": "Alert copies Preferred_Direction; timeframe literal M5, not direction authority"},
            {"file": "market_reviewer/live_hybrid_smc.py", "function": "build_live_hybrid_smc",
             "behavior": "Copies created record direction; does not decide origin direction"},
            {"file": "market_reviewer/live_smc_frozen_context.py", "function": "capture_live_smc_context",
             "behavior": "Freezes origin identity/direction; does not create a new direction"}],
        "change_recommendation": "CONTRACT_REVIEW_REQUIRED; no semantic patch or fabricated SHORT"}


def timeframe_roles():
    return {
        "implemented_groups": {"HTF": list(reviewer.HTF_TIMEFRAMES),
            "TACTICAL": list(reviewer.TACTICAL_TIMEFRAMES), "TRIGGER": list(reviewer.TRIGGER_TIMEFRAMES)},
        "roles": {
            "D1": "Joint Swing Bias authority with H4; external liquidity; macro draw priority; thesis context",
            "H4": "Joint Swing Bias authority; protected structure reversal check; dealing range premium/discount",
            "H1": "Tactical structure/phase; tactical draw priority; no independent direction override",
            "M15": "Tactical phase plus trigger contextual MSS; swing-aligned confirmation",
            "M5": "Tactical/trigger; current price, closed checkpoint and origin snapshot; alert timeframe label"},
        "swing_bias_code": inspect.getsource(reviewer._swing_bias).strip(),
        "origin_timeframe": "M5 checkpoint anchors research origin; eligibility uses combined evidence, not standalone M5 genesis",
        "context_only": "No timeframe is exclusively a context filter: detectors run on all five; direction authority remains D1/H4",
        "proposed_four_layer_contract": "MULTI_TIMEFRAME_ROLE_CONTRACT_MISSING",
        "contract_limit": "Implemented HTF/tactical/trigger groups are not separate macro/swing/tactical-short/execution direction authorities",
        "tactical_short_supported": False,
        "tactical_short_block": "D1 BULLISH + H4 BULLISH/RANGE -> BULLISH; H1 bearish may cause PULLBACK, not SHORT. Research direction follows Swing_Bias; selected displacement and contextual MSS must align with it.",
        "source_functions": ["reviewer._swing_bias", "reviewer._current_phase", "reviewer._liquidity_draws",
            "reviewer._latest_displacement", "reviewer._contextual_mss", "missed_opportunity_live.build_tracker_candidate_from_observation"]}


def _visible(timestamp, timeframe, checkpoint):
    return (isinstance(timestamp, int) and not isinstance(timestamp, bool) and timestamp > 0
            and timeframe in TIMEFRAME_SECONDS and timestamp + TIMEFRAME_SECONDS[timeframe] <= checkpoint)


def tactical_evidence(review, checkpoint):
    """Inspect only exported fields. Counts are observation presence, not unique events."""
    result = {}
    for field in ("Last_MSS", "Last_BOS"):
        value = review.get(field, {}).get("H1") if isinstance(review.get(field), dict) else None
        match = re.fullmatch(r"BEARISH [0-9.]+ @ (\d+)", value or "")
        result["H1_BEARISH_" + field.removeprefix("Last_")] = (
            bool(match and _visible(int(match[1]), "H1", checkpoint)) if value is not None else None)
    displacement = review.get("Displacement")
    # The selected displacement text omits timeframe. Do not invent raw lower-TF events.
    result["EXPOSED_BEARISH_DISPLACEMENT"] = displacement.startswith("BEARISH ") if isinstance(displacement, str) else None
    for field, label in (("FVG", "BEARISH_FVG"), ("Order_Blocks", "BEARISH_OB")):
        values = review.get(field)
        result[label] = any(isinstance(e, dict) and e.get("direction") == "BEARISH"
            and e.get("timeframe") in reviewer.TACTICAL_TIMEFRAMES
            and _visible(e.get("formed_at"), e.get("timeframe"), checkpoint) for e in values) if isinstance(values, list) else None
    events = review.get("Liquidity_Events")
    result["LIQUIDITY_SWEEP_OR_RECLAIM"] = any(isinstance(e, dict)
        and e.get("event_type") in {"SWEPT", "RECLAIMED"}
        and _visible(e.get("timestamp"), e.get("timeframe"), checkpoint) for e in events) if isinstance(events, list) else None
    result["BUYSIDE_SWEEP_OR_RECLAIM"] = any(isinstance(e, dict)
        and ("Buy-side" in str(e.get("level_type")) or e.get("level_type") == "Equal Highs")
        and e.get("event_type") in {"SWEPT", "RECLAIMED"}
        and _visible(e.get("timestamp"), e.get("timeframe"), checkpoint) for e in events) if isinstance(events, list) else None
    return result


def timeframe_direction_audit(journal_path=DEFAULT_JOURNAL, live_store=DEFAULT_LIVE_STORE,
                              root=DEFAULT_ROOT, historical_outcomes=DEFAULT_OUTCOMES):
    result = {"schema": "timeframe-direction-audit.v1", "read_only": True,
        "direction_routing": routing_contract(), "timeframe_roles": timeframe_roles(),
        "assessment": "INSUFFICIENT_EVIDENCE", "assessment_evidence": [], "segments": {}}
    try:
        paths = [Path(journal_path), Path(live_store)]
        before = [p.read_bytes() for p in paths]
        journal, store = [json.loads(b) for b in before]
        gate = research_maturity(root, live_store, historical_outcomes)
        if gate["integrity_reasons"]:
            result["assessment_evidence"] = gate["integrity_reasons"]
            return result
        ids = {o["origin_id"] for o in gate["origin_audit"]}
        records = [r for r in store["records"] if r.get("tracker_id") in ids]
        rows, conflicts = _candidate_rows(journal, records)
        if not rows or conflicts:
            raise ValueError("ARCHIVE_UNAVAILABLE_OR_CONFLICTING")
        by_key = {(r["observation"], r["symbol"]): r for r in rows}
        reviews = {}
        for tx in journal.get("transactions", []):
            if tx.get("status") != "COMPLETE" or tx.get("sample_source") == "HISTORICAL_REPLAY":
                continue
            for symbol, review in (tx.get("recovery_payload", {}).get("reviews") or {}).items():
                key = (tx.get("observation_number"), symbol)
                if key in by_key and by_key[key]["source"] == "COMPLETE_JOURNAL":
                    if key in reviews and reviews[key] != review:
                        raise ValueError("CONFLICTING_EXPOSED_REVIEWS")
                    reviews[key] = review
        for row in rows:
            value = row["bias"] if row["source"] == "COMPLETE_JOURNAL" else (row.get("evidence") or {}).get("SWING_BIAS")
            row["bias_bucket"] = bias_bucket(value)
        start, end = min(r["timestamp"] for r in rows), max(r["timestamp"] for r in rows)
        for name, cutoff in (("latest_24H", end - 86400), ("latest_72H", end - 259200), ("full_window", start)):
            selected = [r for r in rows if cutoff <= r["timestamp"] <= end]
            segment = {"start": cutoff, "end": end, "bias": {}, "tactical_short_context": {}}
            for symbol in ("BTC", "ETH"):
                symbol_rows = [r for r in selected if r["symbol"] == symbol]
                counts = Counter(r["bias_bucket"] for r in symbol_rows)
                segment["bias"][symbol] = {b: counts[b] for b in BIAS_BUCKETS}
                candidate_rows = [r for r in symbol_rows if r["bias_bucket"] in {"BULLISH", "NONE"}]
                examples, unavailable = [], 0
                for row in candidate_rows:
                    review = reviews.get((row["observation"], symbol))
                    if review is None:
                        unavailable += 1
                        continue
                    flags = tactical_evidence(review, row["timestamp"] + 300)
                    examples.append({"observation": row["observation"], "timestamp": row["timestamp"],
                        "bias": row["bias_bucket"], "reviewer_direction": review.get("Preferred_Direction"),
                        "research_direction": row["direction"], "flags": flags})
                names = tactical_evidence({}, 1)
                segment["tactical_short_context"][symbol] = {
                    "denominator": len(candidate_rows), "reviews_unavailable": unavailable,
                    "families": {family: {"count": sum(e["flags"][family] is True for e in examples),
                        "unavailable": unavailable + sum(e["flags"][family] is None for e in examples),
                        "representative_observations": [e["observation"] for e in examples if e["flags"][family] is True][-5:]}
                        for family in names},
                    "POTENTIAL_TACTICAL_SHORT_CONTEXT": sum(any(v is True for k, v in e["flags"].items()
                        if k != "LIQUIDITY_SWEEP_OR_RECLAIM") for e in examples),
                    "examples": [e for e in examples if any(v is True for k, v in e["flags"].items()
                        if k != "LIQUIDITY_SWEEP_OR_RECLAIM")][-3:]}
            result["segments"][name] = segment
        if before != [p.read_bytes() for p in paths]:
            raise ValueError("SOURCE_CHANGED_DURING_READ")
        result["window"] = {"first_observation": min(r["observation"] for r in rows),
            "last_observation": max(r["observation"] for r in rows), "latest_open_timestamp": end,
            "count_unit": "SYMBOL_OBSERVATION_PRESENCE_NOT_INDEPENDENT_SAMPLES_OR_DISTINCT_EVENTS"}
        result["assessment"] = "MIXED_CAUSE"
        result["assessment_evidence"] = ["DIRECTION_DEFAULT_ASYMMETRY: research NONE->LONG while Reviewer NONE->NONE",
            "MULTI_TIMEFRAME_ROLE_CONTRACT_MISSING: no counter-HTF tactical-origin direction authority",
            "These are routing/design limitations, not proof that rejected SHORT trades would qualify or profit"]
        result["limitations"] = ["Exported FVG/OB/liquidity and structure timelines are truncated, not raw detector universe",
            "Repeated events across observations count as exposed presence, not new market events",
            "Bearish/mitigated/invalidated zones are context only, never eligible SHORT or valid setup claims",
            "Selected displacement is bias-filtered and lacks timeframe; zero exposed bearish displacement is not zero raw bearish displacement",
            "No distinct rejection event type is exposed; sweep/reclaim are reported without inventing rejection"]
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        result["segments"] = {}
        result["assessment_evidence"] = ["SOURCE_UNAVAILABLE_INVALID_OR_CHANGED"]
        return result


def render_audit(result):
    lines = ["BIAS DISTRIBUTION"]
    for name, segment in result["segments"].items():
        lines.append(name + ": " + json.dumps(segment["bias"], sort_keys=True))
    lines.extend(["DIRECTION ROUTING", json.dumps(result["direction_routing"], sort_keys=True),
                  "TIMEFRAME ROLES", json.dumps(result["timeframe_roles"], sort_keys=True),
                  "TACTICAL SHORT CONTEXT"])
    for symbol, row in result["segments"].get("full_window", {}).get("tactical_short_context", {}).items():
        lines.append(symbol + ": " + json.dumps(row, sort_keys=True))
    lines.append("ASSESSMENT: " + result["assessment"])
    lines.extend(result["assessment_evidence"])
    return "\n".join(lines)
