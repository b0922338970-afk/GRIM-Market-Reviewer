"""Read-only live acceptance gates; never classify from outcomes or persist state."""
from __future__ import annotations

import json
from pathlib import Path
from statistics import median

from .missed_opportunity import EPISODE_STATUSES, TRACKER_STATUSES
from .smc_live_sample_status import (
    DEFAULT_LIVE_STORE, DEFAULT_OUTCOMES, DEFAULT_ROOT, HORIZONS,
    smc_live_sample_status,
)

SCHEMA = "research-maturity.v1"
LEGACY_MISSING = {
    "MISSING_FROZEN_HYBRID_SMC_STATE", "INCOMPLETE_LIVE_FROZEN_CONTEXT",
    "MISSING_FROZEN_MATCHING_FIELDS", "MISSING_FROZEN_CONTRAST_FIELDS",
}


def _lineage_valid(record):
    origins = [s for s in record.get("snapshots", [])
               if s.get("observation_number") == record.get("origin_observation")
               and s.get("snapshot_timestamp") == record.get("origin_snapshot_timestamp")]
    if not origins or record.get("schema") != "missed-opportunity-tracker.v1":
        return False
    for field in ("production_sequence_id", "production_sequence_state", "production_review_state"):
        value = record.get(field + "_at_origin")
        if not isinstance(value, str) or not value or any(s.get(field) != value for s in origins):
            return False
    return True


def _project(status, records, integrity=()):
    issues = set(integrity)
    if status.get("HISTORICAL_ORIGINS") != 45 or "historical_error" in status:
        issues.add("HISTORICAL_BASELINE_UNVERIFIED")
    unknown = "live_error" in status or not isinstance(status.get("LIVE_ORIGINS"), int)
    if unknown:
        issues.add("LIVE_STORE_UNAVAILABLE")
    by_id = {r["tracker_id"]: r for r in records}
    sides = {d: {"classifiable_live": 0, "outcome_complete": 0, "pending": 0,
                 "unclassifiable": 0, "excluded_invalid": 0,
                 "sample_target_min": 5, "sample_target_preferred": [8, 10],
                 "calibration_target_min": 8, "included_origins": [],
                 "outcome_summaries": {}} for d in ("LONG", "SHORT")}
    audit = []
    for origin in status.get("live_origins", []):
        side = sides[origin["direction"]]
        eid = origin["tracker_id"]
        record = by_id[eid]
        item = {"origin_id": eid, "origin_observation": record.get("origin_observation"),
                "origin_timestamp": origin["origin_timestamp"], "reason": origin.get("reason")}
        audit.append(item)
        if origin["classification"] != "CLASSIFIABLE":
            side["unclassifiable"] += 1
            if origin.get("reason") not in LEGACY_MISSING:
                issues.add("FROZEN_CONTEXT_INTEGRITY_VIOLATION")
            continue
        side["classifiable_live"] += 1
        if not _lineage_valid(record):
            side["excluded_invalid"] += 1
            item["reason"] = "PRODUCTION_RESEARCH_LINEAGE_MISMATCH"
            issues.add(item["reason"])
            continue
        if record.get("episode_status") not in EPISODE_STATUSES or record.get("status") not in TRACKER_STATUSES:
            side["excluded_invalid"] += 1
            item["reason"] = "EPISODE_LIFECYCLE_INVALID"
            issues.add(item["reason"])
            continue
        if origin["OUTCOME_COMPLETE"] is True:
            side["outcome_complete"] += 1
            side["included_origins"].append(eid)
        elif origin["OUTCOME_COMPLETE"] is False:
            side["pending"] += 1
            item["reason"] = "OUTCOME_PENDING"
        else:
            side["excluded_invalid"] += 1
            item["reason"] = "OUTCOME_LIFECYCLE_INVALID"
            issues.add(item["reason"])
    for side in sides.values():
        for horizon in HORIZONS:
            rows = [by_id[eid]["outcomes"][horizon] for eid in side["included_origins"]]
            side["outcome_summaries"][horizon] = {
                "n": len(rows),
                "median_MFE_pct": median(r["MFE_pct"] for r in rows) if rows else None,
                "median_signed_MAE_pct": median(r["MAE_pct"] for r in rows) if rows else None,
            }
        if unknown:
            for key in ("classifiable_live", "outcome_complete", "pending", "unclassifiable", "excluded_invalid"):
                side[key] = None
    first = set(issues)
    calibration = set(issues)
    for direction, side in sides.items():
        count = side["outcome_complete"] or 0
        if count < 5:
            first.add(direction + "_SAMPLE_SHORTFALL")
            if side["pending"]:
                first.add("OUTCOME_PENDING")
        if count < 8:
            calibration.add(direction + "_CALIBRATION_SAMPLE_SHORTFALL")
    if first:
        calibration.add("FIRST_REVIEW_NOT_READY")
    for direction, side in sides.items():
        side["first_review_ready"] = not issues and (side["outcome_complete"] or 0) >= 5
        side["calibration_ready"] = not first and not issues and (side["outcome_complete"] or 0) >= 8
        side["first_review_reasons"] = sorted(issues | ({direction + "_SAMPLE_SHORTFALL"}
            if (side["outcome_complete"] or 0) < 5 else set())) or ["SIDE_COMPLETE_COHORT_MET"]
        side["calibration_reasons"] = sorted(issues | ({"FIRST_REVIEW_NOT_READY"} if first else set())
            | ({direction + "_CALIBRATION_SAMPLE_SHORTFALL"} if (side["outcome_complete"] or 0) < 8 else set())) or ["SIDE_CALIBRATION_INPUTS_MET"]
    return {"schema": SCHEMA, "read_only": True, "FIRST_REVIEW_READY": not first,
            "CALIBRATION_READY": not calibration,
            "FIRST_REVIEW": "NOT_READY" if first else "READY",
            "CALIBRATION": "NOT_READY" if calibration else "READY",
            "first_review_reasons": sorted(first) or ["LIVE_COMPLETE_COHORT_REQUIREMENTS_MET"],
            "calibration_reasons": sorted(calibration) or ["CALIBRATION_INPUT_REQUIREMENTS_MET"],
            "integrity_reasons": sorted(issues), "sides": sides, "origin_audit": audit,
            "excluded_records": status.get("excluded_records", []),
            "execution": "RESERVED"}


def research_maturity(root=DEFAULT_ROOT, live_store=DEFAULT_LIVE_STORE, historical_outcomes=DEFAULT_OUTCOMES):
    """Fail closed on concurrent ledger replacement; no retries, writes or backfill."""
    try:
        before = Path(live_store).read_bytes()
        status = smc_live_sample_status(root, live_store, historical_outcomes)
        records = json.loads(before)["records"]
        if before != Path(live_store).read_bytes():
            return _project({}, [], ["SOURCE_CHANGED_DURING_READ"])
        return _project(status, records)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return _project({}, [], ["SOURCE_UNAVAILABLE_OR_INVALID"])


def render_maturity(result):
    lines = [f"FIRST_REVIEW = {result['FIRST_REVIEW']}", f"CALIBRATION = {result['CALIBRATION']}"]
    for direction, side in result["sides"].items():
        count = side["outcome_complete"]
        count = "N/A" if count is None else count
        lines.append(f"{direction}: classifiable_complete = {count} / 5; preferred = {count} / 8-10")
    blockers = []
    if not result["FIRST_REVIEW_READY"]:
        blockers.extend(result["first_review_reasons"])
    if not result["CALIBRATION_READY"]:
        blockers.extend(result["calibration_reasons"])
    lines.append("BLOCKERS: " + (", ".join(sorted(set(blockers))) if blockers else "NONE"))
    return "\n".join(lines)
