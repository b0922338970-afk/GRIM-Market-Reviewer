"""Read archived non-canonical exposure only; never reconstruct missing history."""
import json
from collections import Counter
from pathlib import Path

from .research_side_diagnostics import DEFAULT_JOURNAL
from .smc_live_sample_status import DEFAULT_LIVE_STORE
from .tactical_evidence_audit import tactical_evidence_audit
from .tactical_direction_shadow import tactical_direction_shadow
from .tactical_provenance import exposure_validation, bind_tactical_identity


def coverage_buckets(trace, review, exposure):
    direction = {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(trace["tactical_direction"])
    validation = exposure_validation(exposure, review, trace["checkpoint"])
    if not validation["valid"]:
        return {"raw": validation["status"], "M15": validation["status"], "readiness": "E_UNAVAILABLE",
                "identity": "TACTICAL_IDENTITY_INCOMPLETE", "validation_reason": validation["reason"]}
    ds = exposure["raw_directional_displacement"]
    raw = ("RAW_MATCHING_DISPLACEMENT_FOUND" if any(e["direction"] == direction for e in ds) else
           "RAW_OPPOSITE_DISPLACEMENT_ONLY" if ds else "RAW_DISPLACEMENT_ABSENT")
    ms = [e for e in exposure["structure_events"] if e["timeframe"] == "M15" and e["type"] in ("MSS", "BOS")]
    m15 = "MATCHING_EVENT_EXISTS" if any(e["direction"] == direction for e in ms) else "OPPOSITE_ONLY" if ms else "NO_EVENT"
    binding = bind_tactical_identity(review, exposure, trace["checkpoint"])
    # Complete raw scope is bounded by the saved prefix, never all historical market data.
    readiness = "A_COMPLETE_EVIDENCE_BOUND" if binding["status"] == "BOUND" else (
        "C_RAW_EVIDENCE_MISSING" if raw == "RAW_DISPLACEMENT_ABSENT" else "B_EVIDENCE_EXISTS_BINDING_INCOMPLETE")
    return {"raw": raw, "M15": m15, "readiness": readiness, "identity": binding["status"]}


def tactical_provenance_status(journal_path=DEFAULT_JOURNAL, live_store=DEFAULT_LIVE_STORE):
    try:
        paths = [Path(journal_path), Path(live_store)]
        before = [p.read_bytes() for p in paths]
        audit = tactical_evidence_audit(journal_path, live_store)
        shadow = tactical_direction_shadow(journal_path, live_store)
        if audit["status"] != "PASS" or shadow["status"] != "PASS":
            raise ValueError("SOURCE_UNAVAILABLE")
        saved = {}
        for tx in json.loads(before[0]).get("transactions", []):
            if tx.get("status") != "COMPLETE" or tx.get("sample_source") == "HISTORICAL_REPLAY":
                continue
            payload = tx.get("recovery_payload", {})
            for symbol, review in payload.get("reviews", {}).items():
                exposure = payload.get("non_canonical_research_evidence", {}).get("tactical_provenance", {}).get(symbol)
                key = (tx["observation_number"], symbol)
                if key in saved and saved[key] != (review, exposure):
                    raise ValueError("CONFLICTING_SOURCE")
                saved[key] = review, exposure
        groups = {"selected_NONE_215": Counter(), "selected_opposite_74": Counter(), "M15_opposite_136": Counter()}
        counter = {s: Counter() for s in ("BTC", "ETH")}
        coverage, identities, validation_reasons = Counter(), Counter(), Counter()
        for trace in audit["traces"]:
            review, exposure = saved.get((trace["observation"], trace["symbol"]), ({}, None))
            bucket = coverage_buckets(trace, review, exposure)
            coverage[bucket["raw"]] += 1
            identities[bucket["identity"]] += 1
            if bucket.get("validation_reason"):
                validation_reasons[bucket["validation_reason"]] += 1
            if trace["observation"] > 276:
                continue
            reason = trace["requirements"]["directional_displacement"]["missing_reason"]
            if trace["shadow_state"] == "TACTICAL_INCOMPLETE":
                if reason == "SELECTED_NONE_RAW_UNKNOWN":
                    groups["selected_NONE_215"][bucket["raw"]] += 1
                if reason == "SELECTED_OPPOSITE_DIRECTION":
                    groups["selected_opposite_74"][bucket["raw"]] += 1
                if "M15_DIRECTIONAL_CONFIRMATION" in trace["missing_evidence"]:
                    groups["M15_opposite_136"][bucket["M15"]] += 1
            if trace["tactical_direction"] == "SHORT" and trace["relationship"] == "COUNTER_TREND":
                counter[trace["symbol"]][bucket["readiness"]] += 1
        if before != [p.read_bytes() for p in paths]:
            raise ValueError("SOURCE_CHANGED")
        return {"schema": "tactical-provenance-status.v1", "status": "PASS", "read_only": True,
                "raw_displacement_coverage": dict(coverage), "baseline_reaudit": {k: dict(v) for k, v in groups.items()},
                "identity_coverage": dict(identities), "countertrend_requalification": {k: dict(v) for k, v in counter.items()},
                "validation_reasons": dict(validation_reasons),
                "segments": shadow["segments"], "liquidity_provenance": "ONLY_EXPLICIT_POOL_ID_AND_EVENT_REFERENCES_BIND",
                "recommendation": "KEEP_SHADOW_ONLY", "scope": "SAVED_PREFIX_ONLY_NO_BACKFILL"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {"schema": "tactical-provenance-status.v1", "status": "UNAVAILABLE", "read_only": True}


def render_status(result):
    return "\n".join(title + "\n" + json.dumps(result.get(key, "UNAVAILABLE"), indent=2, sort_keys=True)
        for title, key in (("RAW DISPLACEMENT COVERAGE", "raw_displacement_coverage"),
            ("M15 EVENT COVERAGE / BASELINE REAUDIT", "baseline_reaudit"), ("LIQUIDITY PROVENANCE", "liquidity_provenance"),
            ("TACTICAL IDENTITY COVERAGE", "identity_coverage"), ("COUNTER-TREND REQUALIFICATION", "countertrend_requalification"),
            ("EXPOSURE VALIDATION REASONS", "validation_reasons"), ("SHADOW CONFIRMED / INCOMPLETE", "segments")))
