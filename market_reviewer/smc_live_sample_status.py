"""Read-only live episode monitoring; no reconstruction or outcome-based membership."""
import json
import math
from pathlib import Path

from .smc_sample_status import (
    CONTRASTS, CONTEXT_FIELDS, DEFAULT_ROOT, NA, _best_cell, _canonical,
    _origins, _read_verified, smc_sample_status,
)

DEFAULT_LIVE_STORE = Path("research/missed-opportunities.json")
DEFAULT_OUTCOMES = Path("research/historical-replay/smc-hybrid-outcome.v1/outcome-join.json")
HORIZONS = ("1H", "4H", "12H", "24H")
ARMS = {
    "MSS": ("MSS", {"CONTEXTUAL"}, {"GENERIC", "GENERIC_INVENTORY"}),
    "BOS": ("BOS", {"CONTEXTUAL"}, {"GENERIC", "GENERIC_INVENTORY"}),
    "FVG_BPR": ("FVG_vs_BPR", {"CHAIN_FVG_NO_BPR"}, {"CHAIN_FVG_PLUS_BPR"}),
    "PARENT": ("parent_authority", {"UNAMBIGUOUS_SINGLE_EVENT_PARENT"}, {"COHERENT_CLUSTER_PARENT"}),
    "OB_BREAKER": ("OB_vs_BREAKER", {"FAILED_OB_WITHOUT_CONFIRMED_BREAKER"}, {"CONFIRMED_BREAKER"}),
    "REACTION": ("reaction", {"SWEEP_RECLAIMED"}, {"SWEEP_ACCEPTED_OUTSIDE"}),
}
ALLOWED = {
    "MSS": {"CONTEXTUAL", "GENERIC", "GENERIC_INVENTORY", "NONE"},
    "BOS": {"CONTEXTUAL", "GENERIC", "GENERIC_INVENTORY", "NONE"},
    "FVG_vs_BPR": {"CHAIN_FVG_NO_BPR", "CHAIN_FVG_PLUS_BPR", "ISOLATED", "OTHER_OR_UNRESOLVED"},
    "parent_authority": {"EXACT_SINGLE_EVENT_PARENT", "UNAMBIGUOUS_SINGLE_EVENT_PARENT", "COHERENT_CLUSTER_PARENT", "AMBIGUOUS_CLUSTER_PARENTS", "AMBIGUOUS_SINGLE_EVENT_PARENTS", "NO_VALID_PARENT", "UNRESOLVED"},
    "OB_vs_BREAKER": {"FAILED_OB_WITHOUT_CONFIRMED_BREAKER", "CONFIRMED_BREAKER", "OTHER_OB_CONTEXT"},
    "reaction": {"SWEEP_RECLAIMED", "SWEEP_ACCEPTED_OUTSIDE", "SWEEP_REJECTED", "SWEEP_UNRESOLVED", "UNRESOLVED"},
}


def _legal_times(value, checkpoint):
    if isinstance(value, list):
        return all(_legal_times(v, checkpoint) for v in value)
    if isinstance(value, dict):
        for k, v in value.items():
            if k in {"available_at", "latest_available_at", "known_at_checkpoint", "as_of", "timestamp", "formed_at", "latest_timestamp", "checkpoint"} and v is not None:
                if not _time(v) or v > checkpoint:
                    return False
            if not _legal_times(v, checkpoint):
                return False
    return True


def _time(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _identity(row, historical=False):
    cp = row.get("checkpoint")
    stamp = cp - 300 if historical and _time(cp) and cp > 300 else None if historical else row.get("origin_snapshot_timestamp")
    return (row.get("symbol"), row.get("direction"), stamp)


def _outcome_ready(record, stamp):
    if not isinstance(record, dict) or not _time(stamp):
        return NA
    outcomes = record.get("outcomes")
    if not isinstance(outcomes, dict):
        return NA
    unknown = False
    for h in HORIZONS:
        value = outcomes.get(h)
        if not isinstance(value, dict):
            unknown = True
            continue
        if value.get("horizon_status") in {"PENDING", "DATA_GAP", "PARTIAL", "UNAVAILABLE"}:
            return False
        metrics = [value.get(k) for k in ("MFE_pct", "MAE_pct")]
        if value.get("horizon_status") != "COMPLETE" or value.get("reference_timestamp") != stamp or value.get("outcome_coverage_complete") is not True or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in metrics):
            unknown = True
    return NA if unknown else True


def _frozen_live(record):
    stamp = record["origin_snapshot_timestamp"]
    if not _time(record.get("origin_observation")):
        raise ValueError("MISSING_ORIGIN_OBSERVATION")
    snapshots = record.get("snapshots")
    if not isinstance(snapshots, list):
        raise ValueError("MISSING_ORIGIN_SNAPSHOT")
    snapshots = [s for s in snapshots if isinstance(s, dict) and s.get("snapshot_timestamp") == stamp and s.get("observation_number") == record.get("origin_observation")]
    unique = {_canonical(s): s for s in snapshots}
    if len(unique) != 1:
        raise ValueError("MISSING_OR_CONFLICTING_ORIGIN_SNAPSHOT")
    snapshot = next(iter(unique.values()))
    # Optional archived origin evidence only. Never write/backfill this field or search later snapshots.
    state = snapshot.get("smc_state")
    if not isinstance(state, dict) or state.get("schema") != "smc-hybrid-outcome.v1":
        raise ValueError("MISSING_FROZEN_HYBRID_SMC_STATE")
    cp = stamp + 300
    if not _time(snapshot.get("available_at")) or snapshot["available_at"] > cp or state.get("checkpoint") != cp:
        raise ValueError("ORIGIN_TEMPORAL_MISMATCH")
    if state.get("episode_id") != record["tracker_id"] or state.get("symbol") != record["symbol"] or state.get("direction") != record["direction"]:
        raise ValueError("ORIGIN_IDENTITY_MISMATCH")
    context = state.get("matching_context")
    fields = state.get("fields")
    if not isinstance(context, dict) or set(context) != CONTEXT_FIELDS or not isinstance(fields, dict):
        raise ValueError("MISSING_FROZEN_MATCHING_FIELDS")
    if any(not isinstance(fields.get(f), str) or not fields[f] for f, _, _ in ARMS.values()):
        raise ValueError("MISSING_FROZEN_CONTRAST_FIELDS")
    if any(fields[f] not in allowed for f, allowed in ALLOWED.items() if f != "reaction") or not set(fields["reaction"].split("+")) <= ALLOWED["reaction"]:
        raise ValueError("UNRECOGNIZED_FROZEN_CONTRAST_STATE")
    if not _legal_times(state, cp):
        raise ValueError("POST_ORIGIN_SMC_EVIDENCE")
    _best_cell([{"context": context, "A": [record["tracker_id"]], "B": []}], {record["tracker_id"]: state})
    return state


def _threshold(known, unknown):
    return True if known >= 5 else NA if unknown else False


def _next(flags):
    ready = sum(v is True for v in flags)
    unknown = sum(v == NA for v in flags)
    return True if ready >= 2 else NA if ready + unknown >= 2 else False


def smc_live_sample_status(root=DEFAULT_ROOT, live_store=DEFAULT_LIVE_STORE, historical_outcomes=DEFAULT_OUTCOMES):
    root, live_store = Path(root), Path(live_store)
    hist = smc_sample_status(root)
    result = {"schema": "smc-sample-status.v2", "monitoring_only": True,
              "historical_source": hist["source_root"], "live_source": str(live_store.resolve()),
              "HISTORICAL_ORIGINS": hist["total_independent_origins"], "LIVE_ORIGINS": NA,
              "TOTAL_INDEPENDENT_ORIGINS": NA, "LONG": NA, "SHORT": NA,
              "CLASSIFIABLE_LIVE_ORIGINS": NA, "UNCLASSIFIABLE_LIVE_ORIGINS": NA,
              "OUTCOME_COMPLETE_LIVE_ORIGINS": NA, "live_origins": [], "excluded_records": [], "contrasts": {}}
    origins, membership, hist_outcomes = {}, {}, {}
    try:
        freeze = json.loads((root / "freeze.json").read_bytes())
        origins = _origins(_read_verified(root, "frozen-states.json", freeze.get("states_sha256")))
        membership = _read_verified(root, "membership.json", freeze.get("membership_sha256"))
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        result["historical_error"] = str(exc)
        membership = {}
    try:
        hist_outcomes = json.loads(Path(historical_outcomes).read_bytes())["records"]
        if not isinstance(hist_outcomes, dict):
            hist_outcomes = {}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    classified, live_records, live_outcomes = {}, {}, {}
    live_unknown = False
    try:
        store = json.loads(live_store.read_bytes())
        if not isinstance(store, dict) or store.get("schema") != "missed-opportunity-tracker.v1" or not isinstance(store.get("records"), list):
            raise ValueError("LIVE_STORE_UNAVAILABLE")
        historical_ids = {i.removeprefix("HIST-") for i in origins}
        historical_keys = {_identity(r, True) for r in origins.values()}
        identities = {}
        conflicts = set()
        if any(not isinstance(r, dict) for r in store["records"]):
            raise ValueError("INVALID_EPISODE_RECORD")
        records = sorted(store["records"], key=lambda r: (str(r.get("tracker_id")), str(r.get("symbol")), str(r.get("direction")), str(r.get("origin_snapshot_timestamp"))))
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("INVALID_EPISODE_RECORD")
            eid = record.get("tracker_id")
            if not isinstance(eid, str) or not eid or record.get("symbol") not in {"BTC", "ETH"} or record.get("direction") not in {"LONG", "SHORT"} or not _time(record.get("origin_snapshot_timestamp")):
                raise ValueError("MISSING_INDEPENDENT_ORIGIN_IDENTITY")
            key = _identity(record)
            if eid in live_records and _identity(live_records[eid]) != key:
                raise ValueError("CONFLICTING_INDEPENDENT_ORIGIN_IDENTITY")
            if eid.removeprefix("HIST-") in historical_ids or key in historical_keys or record.get("sample_source") == "HISTORICAL_REPLAY":
                result["excluded_records"].append({"tracker_id": eid, "reason": "HISTORICAL_OR_DUPLICATE_ORIGIN"})
                continue
            if key in identities or eid in live_records:
                existing = identities.get(key, eid)
                if _canonical(live_records[existing]) != _canonical(record):
                    conflicts.add(existing)
                result["excluded_records"].append({"tracker_id": eid, "reason": "DUPLICATE_LIVE_ORIGIN"})
                continue
            identities[key] = eid
            live_records[eid] = record
        for eid, record in sorted(live_records.items()):
            reason = None
            try:
                if eid in conflicts:
                    raise ValueError("CONFLICTING_DUPLICATE_ORIGIN")
                classified[eid] = _frozen_live(record)
            except (ValueError, TypeError, KeyError) as exc:
                reason = str(exc)
            # Outcome access follows membership classification and never changes it.
            live_outcomes[eid] = NA if eid in conflicts else _outcome_ready(record, record["origin_snapshot_timestamp"])
            result["live_origins"].append({"tracker_id": eid, "symbol": record["symbol"], "direction": record["direction"],
                                          "origin_timestamp": record["origin_snapshot_timestamp"],
                                          "classification": "CLASSIFIABLE" if eid in classified else "LIVE_NOT_CLASSIFIABLE",
                                          "reason": reason, "OUTCOME_COMPLETE": live_outcomes[eid]})
        result.update(LIVE_ORIGINS=len(live_records), CLASSIFIABLE_LIVE_ORIGINS=len(classified),
                      UNCLASSIFIABLE_LIVE_ORIGINS=len(live_records) - len(classified),
                      OUTCOME_COMPLETE_LIVE_ORIGINS=sum(v is True for v in live_outcomes.values()))
        if isinstance(result["HISTORICAL_ORIGINS"], int):
            result["TOTAL_INDEPENDENT_ORIGINS"] = result["HISTORICAL_ORIGINS"] + len(live_records)
            result["LONG"] = hist["LONG"] + sum(r["direction"] == "LONG" for r in live_records.values())
            result["SHORT"] = hist["SHORT"] + sum(r["direction"] == "SHORT" for r in live_records.values())
    except (OSError, ValueError, TypeError) as exc:
        result["live_error"] = str(exc)
        live_unknown = True
        classified, live_records, live_outcomes = {}, {}, {}
        result["live_origins"] = []
    unclassified = set(live_records) - set(classified)
    for name in CONTRASTS:
        historical = hist["contrasts"][name]
        groups = {}
        historical_available = historical["availability"] == "AVAILABLE" and "historical_error" not in result
        if historical_available:
            for group in membership.get(name, []):
                groups[_canonical(group["context"])] = {"context": group["context"], "historical": {a: group[a] for a in ("A", "B")}, "live": {"A": [], "B": []}}
        field, aset, bset = ARMS[name]
        for eid, state in sorted(classified.items()):
            if state["phase"] not in {"CONTINUATION", "PULLBACK", "REVERSAL_CANDIDATE", "EXHAUSTION"}:
                continue
            value = state["fields"][field]
            arm = "A" if value in aset else "B" if value in bset else None
            if arm is None:
                continue
            key = _canonical(state["matching_context"])
            groups.setdefault(key, {"context": state["matching_context"], "historical": {"A": [], "B": []}, "live": {"A": [], "B": []}})["live"][arm].append(eid)
        candidates = [g for g in groups.values() if any(g[s][a] for s in ("historical", "live") for a in ("A", "B"))]
        def support(g, a):
            return len(g["historical"][a]) + len(g["live"][a])
        best = min(candidates, key=lambda g: (-min(support(g, "A"), support(g, "B")), -support(g, "A") - support(g, "B"), _canonical(g["context"]))) if candidates else None
        unknown = live_unknown or bool(unclassified) or not historical_available
        cell = {"label": CONTRASTS[name], "context": best["context"] if best else NA,
                "target_per_side": 5, "preferred_per_side": [8, 10], "LIVE_NOT_CLASSIFIABLE": len(unclassified) if not live_unknown else NA,
                "historical": {}, "live": {}, "combined": {}, "known_classifiable_live": {}, "known_combined": {}, "outcome_complete": {}}
        sample_flags, outcome_flags = [], []
        for a in ("A", "B"):
            hids = best["historical"][a] if best else []
            lids = best["live"][a] if best else []
            h, l = len(hids), len(lids)
            cell["historical"][a] = h if historical_available and best else NA
            cell["known_classifiable_live"][a] = l if not live_unknown else NA
            cell["known_combined"][a] = h + l if best else NA
            cell["live"][a] = NA if live_unknown or unclassified else l
            cell["combined"][a] = NA if unknown or not best else h + l
            statuses = [_outcome_ready(hist_outcomes.get(i, {}), _identity(origins[i], True)[2]) for i in hids] + [live_outcomes[i] for i in lids]
            complete = sum(v is True for v in statuses)
            cell["outcome_complete"][a] = complete if best else NA
            sample_flags.append(_threshold(h + l, unknown or not best))
            outcome_flags.append(_threshold(complete, unknown or not best or NA in statuses))
        cell["MATCHED_SAMPLE_READY"] = False if False in sample_flags else NA if NA in sample_flags else True
        cell["OUTCOME_READY"] = False if False in outcome_flags else NA if NA in outcome_flags else True
        cell["READY"] = cell["MATCHED_SAMPLE_READY"]
        result["contrasts"][name] = cell
    result["NEXT_REVIEW_READY"] = {"SAMPLE_READY": _next([v["MATCHED_SAMPLE_READY"] for v in result["contrasts"].values()]),
                                   "OUTCOME_READY": _next([v["OUTCOME_READY"] for v in result["contrasts"].values()])}
    result["TOTAL_CONTRAST_COUNT"] = len(CONTRASTS)
    result["READY_CONTRAST_COUNT"] = {}
    for label, field in (("SAMPLE_READY", "MATCHED_SAMPLE_READY"), ("OUTCOME_READY", "OUTCOME_READY")):
        flags = [v[field] for v in result["contrasts"].values()]
        result["READY_CONTRAST_COUNT"][label] = NA if NA in flags else sum(v is True for v in flags)
    return result
