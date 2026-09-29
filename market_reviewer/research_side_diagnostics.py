"""Read-only archive diagnostics, never replay or backfill origin eligibility."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from statistics import mean, median

from .missed_opportunity import TERMINAL_PRODUCTION_STATES, is_eligible_origin
from .missed_opportunity_live import _direction_from_review, _new_legal_genesis_in_review
from .research_maturity import research_maturity
from .smc_live_sample_status import DEFAULT_LIVE_STORE, DEFAULT_OUTCOMES, DEFAULT_ROOT

UNAVAILABLE = "UNAVAILABLE"
DEFAULT_JOURNAL = Path("artifact/observation-commit-journal.json")
DOMAINS = {"structure": "STRUCTURE", "momentum": "MOMENTUM",
           "positioning": "POSITIONING", "liquidity": "LIQUIDITY"}


def _positive_int(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _measure(values):
    known = [v for v in values if v is not None]
    return {"count": sum(known) if len(known) == len(values) else UNAVAILABLE,
            "observed_count": sum(known), "known_denominator": len(known),
            "total_denominator": len(values)}


def _candidate_rows(journal, records):
    rows, conflicts = {}, set()

    def add(key, row):
        if key in rows and rows[key] != row:
            conflicts.add(key)
        else:
            rows[key] = row

    for tx in journal.get("transactions", []):
        if not isinstance(tx, dict) or tx.get("status") != "COMPLETE" or tx.get("sample_source") == "HISTORICAL_REPLAY":
            continue
        payload = tx.get("recovery_payload") or {}
        obs, checkpoint = tx.get("observation_number"), tx.get("canonical_checkpoint")
        if not _positive_int(obs) or not _positive_int(checkpoint) or payload.get("observation_number") != obs:
            continue
        for symbol, review in (payload.get("reviews") or {}).items():
            if symbol not in {"BTC", "ETH"} or not isinstance(review, dict):
                continue
            opportunity = (payload.get("opportunity_snapshots") or {}).get(symbol, {})
            stamp = opportunity.get("snapshot_timestamp")
            if not _positive_int(stamp) or stamp + 300 != checkpoint or str(stamp) != str(review.get("Review_Timestamp")):
                continue
            direction = _direction_from_review(review) if isinstance(review.get("Swing_Bias"), str) else None
            risks = opportunity.get("risk_signatures")
            add((obs, symbol), {"observation": obs, "symbol": symbol, "timestamp": stamp,
                "direction": direction, "evidence": None, "source": "COMPLETE_JOURNAL",
                "bias": review.get("Swing_Bias"), "candidate": {
                    "production_sequence_state": review.get("Sequence_State"),
                    "new_legal_genesis_active": _new_legal_genesis_in_review(review)
                        if isinstance(review.get("Sequence_Transitions"), list) else None,
                    "risk_signatures": risks if isinstance(risks, list) else None}})
    # Tracker snapshots preserve actual enriched research evidence, but are selected
    # by lifecycle survival. Never treat this subset as the full candidate universe.
    for record in records:
        for snapshot in record.get("snapshots", []):
            obs, stamp = snapshot.get("observation_number"), snapshot.get("snapshot_timestamp")
            if not _positive_int(obs) or not _positive_int(stamp):
                continue
            key = (obs, record["symbol"])
            available = snapshot.get("available_at")
            if not _positive_int(available) or available > stamp + 300:
                conflicts.add(key)
                continue
            evidence = snapshot.get("opportunity_evidence")
            if key not in rows:
                rows[key] = {"observation": obs, "symbol": record["symbol"], "timestamp": stamp,
                    "direction": _direction_from_review({"Swing_Bias": (evidence or {}).get("SWING_BIAS")}) if isinstance((evidence or {}).get("SWING_BIAS"), str) else None,
                    "persisted_origin_direction": record["direction"], "bias": (evidence or {}).get("SWING_BIAS"), "candidate": {},
                    "evidence": evidence, "source": "TRACKER_SNAPSHOT_ONLY"}
            elif rows[key]["timestamp"] != stamp:
                conflicts.add(key)
            elif rows[key]["evidence"] is not None and rows[key]["evidence"] != evidence:
                conflicts.add(key)
            else:
                rows[key]["evidence"] = evidence
    for key in conflicts:
        rows.pop(key, None)
    return sorted(rows.values(), key=lambda r: (r["timestamp"], r["observation"], r["symbol"])), len(conflicts)


def _facts(row):
    evidence, candidate = row.get("evidence") or {}, row["candidate"]
    components = {k: evidence[v] == "POSITIVE" if isinstance(evidence.get(v), str) else None
                  for k, v in DOMAINS.items()}
    score = (2 * components["structure"] + 2 * components["momentum"]
             + components["positioning"] + components["liquidity"]) if all(v is not None for v in components.values()) else None
    weighted = is_eligible_origin({"production_sequence_state": "INVALIDATED",
        "opportunity_evidence": evidence}) if score is not None else None
    reasons = []
    if candidate.get("production_sequence_state") not in TERMINAL_PRODUCTION_STATES and isinstance(candidate.get("production_sequence_state"), str):
        reasons.append("PRODUCTION_CONTEXT_ACTIVE")
    if candidate.get("new_legal_genesis_active") is True:
        reasons.append("PRODUCTION_GENESIS_ACTIVE")
    if "HARD_RESEARCH_INVALIDATION" in (candidate.get("risk_signatures") or []):
        reasons.append("HARD_RESEARCH_INVALIDATION")
    if weighted is False:
        reasons.append("WEIGHTED_SCORE_SHORTFALL")
    complete = score is not None and isinstance(candidate.get("production_sequence_state"), str) and isinstance(candidate.get("new_legal_genesis_active"), bool) and isinstance(candidate.get("risk_signatures"), list)
    eligible = is_eligible_origin(dict(candidate, opportunity_evidence=evidence)) if complete else False if reasons else None
    return components, score, weighted, eligible, reasons


def _segment(rows, origins, cutoff, end):
    selected = [r for r in rows if cutoff <= r["timestamp"] <= end]
    result = {"start": cutoff, "end": end, "sides": {},
              "no_direction_non_candidates": sum(r["direction"] == "NONE" for r in selected),
              "direction_unavailable": sum(r["direction"] is None for r in selected)}
    for direction in ("LONG", "SHORT"):
        candidates = [r for r in selected if r["direction"] == direction]
        facts = [_facts(r) for r in candidates]
        cohort = [o for o in origins if o["direction"] == direction and cutoff <= o["origin_timestamp"] <= end]
        scores = [f[1] for f in facts if f[1] is not None]
        blockers = {}
        created = {(o["origin_observation"], o["symbol"]) for o in origins}
        rejected = [(r, f) for r, f in zip(candidates, facts) if (r["observation"], r["symbol"]) not in created]
        for row, fact in rejected:
            for reason in fact[4]:
                blockers.setdefault(reason, []).append(row["timestamp"])
        reason_rows = [{"reason": reason, "count": len(times),
            "percentage": 100 * len(times) / len(rejected),
            "first_seen": min(times), "last_seen": max(times)} for reason, times in sorted(blockers.items())]
        eligible = _measure([f[3] for f in facts])
        classifiable = sum(o["classifiable"] for o in cohort)
        result["sides"][direction] = {
            "candidate": len(candidates),
            "explicit_swing_bias_counts": dict(sorted(Counter(r["bias"] or "NOT_ARCHIVED" for r in candidates).items())),
            **{k + "_qualified": _measure([f[0][k] for f in facts]) for k in DOMAINS},
            "weighted_threshold": _measure([f[2] for f in facts]),
            "research_origin_eligible": eligible,
            "eligibility_scope": "CURRENT_UNCHANGED_PREDICATES_ON_ARCHIVED_EVIDENCE; NOT_HISTORICAL_DECISION_REPLAY",
            "legal_genesis_eligible": UNAVAILABLE,
            "origin_created": len(cohort), "frozen_context_valid": classifiable,
            "classifiable": classifiable, "outcome_complete": sum(o["complete"] for o in cohort),
            "score_distribution": dict(sorted(Counter(scores).items())),
            "score_mean": mean(scores) if scores else UNAVAILABLE,
            "score_median": median(scores) if scores else UNAVAILABLE,
            "score_coverage": {"known": len(scores), "candidates": len(candidates)},
            "score_scope": "PERSISTED_EVIDENCE_SUBSET_ONLY",
            "origin_conversion_rate": len(cohort) / len(candidates) if candidates else UNAVAILABLE,
            "classifiable_conversion_rate": classifiable / len(candidates) if candidates else UNAVAILABLE,
            "conversion_denominator": "ARCHIVED_SYMBOL_OBSERVATION_CANDIDATES; ORIGINS_ARE_INDEPENDENT",
            "rejections": reason_rows, "rejection_denominator": len(rejected),
            "rejection_semantics": "NON_EXCLUSIVE_EXISTING_ELIGIBILITY_PREDICATES; NOT_RECORDED_DECISION_REASONS",
            "formal_rejection_report": UNAVAILABLE,
            "unexplained_non_origins": sum(not f[4] for _, f in rejected),
        }
    return result


def research_side_diagnostics(journal_path=DEFAULT_JOURNAL, live_store=DEFAULT_LIVE_STORE,
                              root=DEFAULT_ROOT, historical_outcomes=DEFAULT_OUTCOMES):
    result = {"schema": "research-side-diagnostics.v1", "read_only": True,
        "view": "CORRECTED_DIAGNOSTIC_VIEW",
        "origin_count_authority": "PERSISTED_ORIGINS_UNCHANGED; NOT_RECLASSIFIED",
        "assessment": "INSUFFICIENT_EVIDENCE_TO_DIAGNOSE", "assessment_reasons": [],
        "segments": {}}
    try:
        paths = [Path(journal_path), Path(live_store)]
        before = [p.read_bytes() for p in paths]
        journal, store = [json.loads(b) for b in before]
        gate = research_maturity(root, live_store, historical_outcomes)
        if gate["integrity_reasons"]:
            result["assessment_reasons"] = gate["integrity_reasons"]
            return result
        ids = {o["origin_id"] for o in gate["origin_audit"]}
        records = {r["tracker_id"]: r for r in store["records"] if r.get("tracker_id") in ids}
        legacy_audit = []
        for eid, record in sorted(records.items()):
            snapshots = [s for s in record.get("snapshots", []) if
                s.get("observation_number") == record.get("origin_observation") and
                s.get("snapshot_timestamp") == record.get("origin_snapshot_timestamp")]
            biases = {(s.get("opportunity_evidence") or {}).get("SWING_BIAS") for s in snapshots}
            bias = next(iter(biases)) if len(biases) == 1 else None
            legacy_audit.append({"origin_id": eid, "persisted_direction": record["direction"],
                "creation_swing_bias": bias or UNAVAILABLE,
                "routing_artifact": "LEGACY_DIRECTION_ROUTING_ARTIFACT" if record["direction"] == "LONG" and bias == "NONE" else None,
                "classifiable_complete": eid in gate["sides"][record["direction"]]["included_origins"]})
        result["legacy_origin_audit"] = legacy_audit
        origins = []
        for o in gate["origin_audit"]:
            record = records[o["origin_id"]]
            origins.append(dict(o, symbol=record["symbol"], direction=record["direction"],
                classifiable=o["reason"] in {None, "OUTCOME_PENDING"},
                complete=o["origin_id"] in gate["sides"][record["direction"]]["included_origins"]))
        rows, conflicts = _candidate_rows(journal, list(records.values()))
        if before != [p.read_bytes() for p in paths]:
            raise ValueError("SOURCE_CHANGED_DURING_READ")
        if not rows:
            raise ValueError("NO_ARCHIVED_LIVE_CANDIDATES")
        start, end = min(r["timestamp"] for r in rows), max(r["timestamp"] for r in rows)
        observations = {r["observation"] for r in rows}
        keys = {(r["observation"], r["symbol"]) for r in rows}
        missing = sum((obs, s) not in keys for obs in range(min(observations), max(observations) + 1) for s in ("BTC", "ETH"))
        result.update(window={"first_observation": min(observations), "last_observation": max(observations),
            "first_timestamp": start, "last_timestamp": end, "missing_symbol_observations": missing,
            "conflicting_rows": conflicts, "unknown_direction_rows": sum(r["direction"] is None for r in rows),
            "scope": "AVAILABLE_FORWARD_LIVE_ARCHIVE; NOT_ASSUMED_FULL_RETENTION"},
            direction_contract="BULLISH -> LONG; BEARISH -> SHORT; other explicit bias -> NONE (non-candidate)",
            gate={k: gate[k] for k in ("FIRST_REVIEW", "CALIBRATION", "first_review_reasons")})
        for name, cutoff in (("latest_24H", end - 86400), ("latest_72H", end - 259200), ("full_window", start)):
            result["segments"][name] = _segment(rows, origins, cutoff, end)
        result["assessment_reasons"] = ["UNRETAINED_REJECTED_CANDIDATE_ENRICHMENT",
            "FORMAL_ORIGIN_REJECTION_REPORT_UNAVAILABLE", "NO_MATCHED_DIRECTIONAL_PIPELINE_CONTROL"]
        if missing or conflicts:
            result["assessment_reasons"].append("INCOMPLETE_OR_CONFLICTING_ARCHIVE")
        # A directional count skew alone cannot distinguish market opportunity from
        # routing or enrichment asymmetry. No causal label without those controls.
        return result
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        result["segments"] = {}
        result["assessment_reasons"] = ["SOURCE_UNAVAILABLE_INVALID_OR_CHANGED"]
        return result


def render_diagnostics(result):
    lines = ["LIVE SIDE DIAGNOSTICS", result.get("view", "CORRECTED_DIAGNOSTIC_VIEW"),
             "Origin counts remain persisted; candidates use corrected routing."]
    for name, segment in result["segments"].items():
        lines.append(name)
        lines.append(f"NONE non-candidate={segment['no_direction_non_candidates']}")
        for side, row in segment["sides"].items():
            lines.append(f"{side}: candidate={row['candidate']} eligible={row['research_origin_eligible']['count']} origin={row['origin_created']} classifiable={row['classifiable']} complete={row['outcome_complete']}")
    full = result["segments"].get("full_window", {}).get("sides", {}).get("SHORT", {})
    lines.append("SHORT TOP BLOCKERS (supported predicates; formal report unavailable):")
    lines.extend(f"{r['reason']}: {r['count']} ({r['percentage']:.2f}%)" for r in full.get("rejections", []))
    if not full.get("rejections"):
        lines.append(UNAVAILABLE)
    lines.append("ASSESSMENT: " + result["assessment"])
    lines.extend(result["assessment_reasons"])
    return "\n".join(lines)
