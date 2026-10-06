"""Read-only acceptance attribution from durable forward receipts only."""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from . import reviewer as rv
from . import tactical_shadow_capture as capture
from .model import TIMEFRAMES
from .tactical_sequence_shadow import BIAS, _inputs, _level
from .tactical_direction_shadow import classify_tactical

FIRST_OBSERVATION = 301
LAST_OBSERVATION = 419
STAGES = (
    "SEQUENCE_STARTED", "TARGET_SELECTED", "POST_SELECTION_SWEEP_SEEN",
    "MATCHING_DISPLACEMENT_SEEN", "CONTEXTUAL_MSS_CANDIDATE",
    "CONTEXTUAL_MSS_ACCEPTED", "MSS_CONFIRMED", "SETUP_FVG_FOUND",
    "SETUP_FORMED", "RETEST_PENDING", "RETEST_CONFIRMED",
)


def _reference(event):
    return {k: event.get(k) for k in ("event_id", "type", "timeframe", "timestamp", "available_at",
                                    "direction", "pool_id")} | {"evidence": event["evidence"]}


def _unique(events):
    found = {}
    for event in events:
        key = capture.digest({k: event.get(k) for k in ("type", "timeframe", "timestamp", "pool_id", "direction", "evidence")})
        found.setdefault(key, event)
    return list(found.values())


def _probe(inputs, target, direction):
    """Observe candidates without changing ancestry or the lifecycle."""
    raw = [e for e in inputs["liquidity_events"] if e["type"] == "SWEPT"]
    pool = [e for e in raw if e.get("pool_id") == target["liquidity_id"]]
    level = _level(target)
    exact = [e for e in pool if rv.LiquidityEvent(**e["evidence"]) in
             rv._events_for_level([rv.LiquidityEvent(**e["evidence"])], level)]
    post = [e for e in exact if e["timestamp"] > target["selected_at"]]
    result = {"raw_sweeps": [_reference(e) for e in raw],
              "raw_inventory_not_linkage": {
                  "displacements": {tf: sum(e["timeframe"] == tf for e in inputs["raw_directional_displacement"])
                                    for tf in TIMEFRAMES},
                  "trigger_mss": {tf: sum(e["timeframe"] == tf and e["type"] == "MSS"
                                          for e in inputs["structure_events"]) for tf in rv.TRIGGER_TIMEFRAMES}},
              "target_sweeps": [_reference(e) for e in exact],
              "post_selection_sweeps": [_reference(e) for e in post],
              "identity_mismatched_sweeps": [_reference(e) for e in pool if e not in exact],
              "selected_sweep": None, "raw_displacements_after_sweep": [],
              "matching_displacements": [], "selected_displacement": None,
              "raw_mss_after_sweep": [], "eligible_mss_candidates": [],
              "producer_mss": None, "mss_identity_accepted": False,
              "setup_fvg": None, "setup_anchor_valid": False, "producer_blocker": None}
    if not post:
        return result
    latest = max(e["timestamp"] for e in post)
    latest_sweeps = [e for e in post if e["timestamp"] == latest]
    if len(latest_sweeps) != 1:
        result["producer_blocker"] = "SWEEP_IDENTITY_AMBIGUOUS"
        return result
    sweep = latest_sweeps[0]
    result["selected_sweep"] = _reference(sweep)
    ds = [e for e in inputs["raw_directional_displacement"] if e["timestamp"] > latest]
    result["raw_displacements_after_sweep"] = [_reference(e) for e in ds]
    bias = BIAS[direction]
    valid = [e for e in ds if e["direction"] == bias and e["evidence"]["strength"] in {"VALID", "STRONG"}]
    result["matching_displacements"] = [_reference(e) for e in valid]
    displacement = rv._latest_displacement([rv.DisplacementEvent(**e["evidence"]) for e in ds], bias)
    matches = [e for e in ds if displacement and e["evidence"] == asdict(displacement)]
    if len(matches) != 1:
        result["producer_blocker"] = "DISPLACEMENT_LINKAGE_MISSING_OR_AMBIGUOUS"
        return result
    result["selected_displacement"] = _reference(matches[0])
    structures = {tf: SimpleNamespace(events=[rv.StructureEvent(**e["evidence"])
                  for e in inputs["structure_events"] if e["timeframe"] == tf]) for tf in TIMEFRAMES}
    raw_mss = [e for e in inputs["structure_events"] if e["type"] == "MSS"
               and e["timeframe"] in rv.TRIGGER_TIMEFRAMES and e["timestamp"] > latest]
    result["raw_mss_after_sweep"] = [_reference(e) for e in raw_mss]
    eligible = [e for e in raw_mss if e["direction"] == bias and e["timestamp"] >= displacement.timestamp]
    result["eligible_mss_candidates"] = [_reference(e) for e in eligible]
    mss = rv._contextual_mss(structures, rv.LiquidityEvent(**sweep["evidence"]), displacement, bias)
    if mss is None:
        result["producer_blocker"] = "CONTEXTUAL_MSS_MISSING"
        return result
    result["producer_mss"] = asdict(mss)
    anchors = []
    for tf in rv.TRIGGER_TIMEFRAMES:
        anchors = [e for e in inputs["structure_events"] if e["timeframe"] == tf and e["type"] == "MSS"
                   and e["timestamp"] == mss.timestamp and e["direction"] == mss.direction
                   and e["evidence"].get("price") == mss.price]
        if anchors:
            break
    result["mss_identity_accepted"] = len(anchors) == 1
    if len(anchors) != 1:
        result["producer_blocker"] = "CONTEXTUAL_MSS_IDENTITY_AMBIGUOUS"
        return result
    gap = rv._setup_fvg([rv.FairValueGap(**e["evidence"]) for e in inputs["zones"]],
                        rv.LiquidityEvent(**sweep["evidence"]), displacement, mss, bias)
    if gap:
        result["setup_fvg"] = asdict(gap)
        result["setup_anchor_valid"] = (gap.related_displacement_id == f"{matches[0]['timeframe']}:{matches[0]['timestamp']}"
            and gap.status != "INVALIDATED"
            and sum(e["evidence"] == asdict(gap) for e in inputs["zones"]) == 1)
    return result


def _first_blocker(sequence):
    passed = sequence["funnel"]
    probes = [p["evidence"] for p in sequence["observations"] if p["within_lifecycle"]]
    if not passed["POST_SELECTION_SWEEP_SEEN"]:
        if any(p["identity_mismatched_sweeps"] for p in probes):
            return "SWEEP_IDENTITY_MISMATCH"
        if any(p["target_sweeps"] for p in probes):
            return "SWEEP_NOT_POST_SELECTION"
        return "INVALIDATED_BEFORE_PROGRESS" if sequence["invalidation"] else "TARGET_NEVER_SWEPT"
    if not passed["MATCHING_DISPLACEMENT_SEEN"]:
        ds = [e for p in probes for e in p["raw_displacements_after_sweep"]]
        if ds and not any(e["direction"] == BIAS[sequence["direction"]] for e in ds):
            return "DISPLACEMENT_DIRECTION_MISMATCH"
        return "SWEEP_SEEN_NO_MATCHING_DISPLACEMENT"
    if not passed["CONTEXTUAL_MSS_CANDIDATE"]:
        return "CONTEXTUAL_MSS_NOT_PRODUCED"
    if not passed["CONTEXTUAL_MSS_ACCEPTED"]:
        if any(p["producer_mss"] and not p["mss_identity_accepted"] for p in probes):
            return "CONTEXTUAL_MSS_IDENTITY_LINK_FAILED"
        return "INVALIDATED_BEFORE_PROGRESS" if sequence["invalidation"] else "STATE_MACHINE_BLOCK"
    if not passed["MSS_CONFIRMED"]:
        return "STATE_MACHINE_BLOCK"
    if not passed["SETUP_FVG_FOUND"]:
        return "MSS_ACCEPTED_NO_SETUP_FVG"
    if not passed["SETUP_FORMED"]:
        return "SETUP_FVG_ANCHOR_FAILED"
    if not passed["RETEST_CONFIRMED"]:
        return "RETEST_NOT_OCCURRED"
    return None


def _summarize_sequence(sequence):
    rows = sequence["observations"]
    transitions = sequence["transitions"]
    invalidations = [t for t in transitions if t["to_state"] == "INVALIDATED"]
    sequence["invalidation"] = invalidations[0] if invalidations else None
    if invalidations:
        row = next(r for r in rows if r["observation"] == invalidations[0]["observation"])
        sequence["invalidation"] = {**invalidations[0], "direction_before": sequence["direction"],
            "direction_at_invalidation": row["observed_tactical_direction"],
            "direction_evidence": row["direction_evidence"]}
    end = sequence["invalidation"]["observation"] if invalidations else rows[-1]["observation"]
    sequence["end_observation"] = end
    sequence["latest_observation"] = rows[-1]["observation"]
    sequence["state"] = "INVALIDATED" if invalidations else rows[-1]["state"]
    sequence["current_blocker"] = invalidations[0]["reason"] if invalidations else rows[-1]["blockers"]
    for row in rows:
        row["within_lifecycle"] = row["observation"] <= end
    active = [r for r in rows if r["within_lifecycle"]]
    eligible = [r for r in active if r["observation"] > sequence["start_observation"]]
    probes = [r["evidence"] for r in eligible]
    confirmed = {t["to_state"] for t in transitions}
    sequence["funnel"] = dict(zip(STAGES, (
        True, sequence["target_selection_legal"],
        any(p["post_selection_sweeps"] for p in probes),
        any(p["selected_displacement"] for p in probes),
        any(p["producer_mss"] for p in probes),
        any(r["contextual_mss"] for r in active),
        "MSS_CONFIRMED" in confirmed,
        "MSS_CONFIRMED" in confirmed and any(p["setup_fvg"] for p in probes),
        "SETUP_FORMED" in confirmed, "RETEST_PENDING" in confirmed, "RETEST_CONFIRMED" in confirmed,
    )))
    sequence["first_missing_stage"] = next((s for s in STAGES if not sequence["funnel"][s]), None)
    sequence["first_blocker"] = _first_blocker(sequence)
    sequence["target_identity_preserved"] = all(r["target"] == sequence["selected_target"] for r in rows)
    sequence["target_audit"] = {
        "post_selection_sweep_during_lifecycle": sequence["funnel"]["POST_SELECTION_SWEEP_SEEN"],
        "post_terminal_sweeps": _unique([e for r in rows if not r["within_lifecycle"]
                                          for e in r["evidence"]["post_selection_sweeps"]]),
        "post_terminal_matching_displacements": _unique([e for r in rows if not r["within_lifecycle"]
                                                          for e in r["evidence"]["matching_displacements"]]),
        "post_terminal_mss_candidates": _unique([e for r in rows if not r["within_lifecycle"]
                                                 for e in r["evidence"]["eligible_mss_candidates"]]),
        "pre_or_at_selection_sweeps": _unique([e for r in rows for e in r["evidence"]["target_sweeps"]
                                               if e["timestamp"] <= sequence["selected_target"]["selected_at"]]),
        "lifecycle_sweep_consumed": any(r["selected_sweep"] for r in active),
        "target_sweep_binding_gap": any(r["evidence"]["post_selection_sweeps"] and
            r["lifecycle_evaluated"] and not r["selected_sweep"] and
            "MATCHING_POST_SELECTION_SWEEP_MISSING" in r["blockers"] for r in eligible),
    }
    sequence["displacement_audit"] = {
        "status": ("NOT_APPLICABLE_NO_LIVE_SWEEP" if not sequence["funnel"]["POST_SELECTION_SWEEP_SEEN"] else
                   "MATCHING_DISPLACEMENT_PRESENT" if sequence["funnel"]["MATCHING_DISPLACEMENT_SEEN"] else
                   "DISPLACEMENT_EXISTS_BUT_NOT_BOUND" if any(p["matching_displacements"] for p in probes) else
                   "NO_DISPLACEMENT_FORMED_IN_FROZEN_SCOPE"),
        "matching_evidence": _unique([e for p in probes for e in p["matching_displacements"]]),
    }
    sequence["mss_audit"] = {
        "status": ("NOT_REACHED_SWEEP_DISPLACEMENT_PREREQUISITES" if not sequence["funnel"]["MATCHING_DISPLACEMENT_SEEN"] else
                   "ACCEPTED" if sequence["funnel"]["CONTEXTUAL_MSS_ACCEPTED"] else
                   "NOT_ACCEPTED_LIFECYCLE_INVALIDATED" if sequence["invalidation"] and
                       any(p["producer_mss"] for p in probes) and
                       not any(r["lifecycle_evaluated"] and r["evidence"]["producer_mss"] for r in eligible) else
                   "MSS_EXISTS_LINKAGE_FAILED" if any(p["producer_mss"] for p in probes) else
                   "PRODUCER_DID_NOT_RETURN_MSS" if any(p["raw_mss_after_sweep"] for p in probes) else
                   "MARKET_MSS_ABSENT_IN_FROZEN_SCOPE"),
        "raw_evidence": _unique([e for p in probes for e in p["raw_mss_after_sweep"]]),
        "eligible_candidates": _unique([e for p in probes for e in p["eligible_mss_candidates"]]),
    }
    return sequence


def _churn(sequences):
    same_target = same_direction = replacement = unexplained = 0
    for symbol in capture.SYMBOLS:
        own = [s for s in sequences if s["symbol"] == symbol]
        seen = set()
        for index, s in enumerate(own):
            target = s["selected_target_id"]
            same_target += target in seen
            if target in seen:
                prior = next(x for x in reversed(own[:index]) if x["selected_target_id"] == target)
                unexplained += not prior["invalidation"]
            seen.add(target)
            if index:
                previous = own[index - 1]
                same_direction += previous["direction"] == s["direction"]
                replacement += previous["selected_target_id"] != target
                unexplained += not previous["invalidation"]
    return {"unique_target_identities": len({(s["symbol"], s["selected_target_id"]) for s in sequences}),
            "unique_sequence_identities": len({s["sequence_id"] for s in sequences}),
            "same_target_restart_count": same_target, "same_direction_restart_count": same_direction,
            "replacement_count": replacement, "unexplained_restart_count": unexplained,
            "SHADOW_SEQUENCE_CHURN": bool(unexplained)}


def _assessment(sequences, churn):
    causes = set()
    if churn["SHADOW_SEQUENCE_CHURN"] or any(not s["target_identity_preserved"] for s in sequences):
        causes.add("SEQUENCE_STATE_MACHINE_GAP")
    for s in sequences:
        sequence_gap = False
        if s["target_audit"]["target_sweep_binding_gap"]:
            causes.add("TARGET_SWEEP_BINDING_GAP")
            sequence_gap = True
        if s["displacement_audit"]["status"] == "DISPLACEMENT_EXISTS_BUT_NOT_BOUND":
            causes.add("DISPLACEMENT_BINDING_GAP")
            sequence_gap = True
        if s["mss_audit"]["status"] in {"PRODUCER_DID_NOT_RETURN_MSS", "MSS_EXISTS_LINKAGE_FAILED"}:
            causes.add("CONTEXTUAL_MSS_PRODUCER_GAP")
            sequence_gap = True
        if not sequence_gap and not s["funnel"]["MSS_CONFIRMED"]:
            causes.add("GENUINE_MARKET_NO_PROGRESSION")
    if len(causes) > 1:
        return "MIXED_LIVE_SEQUENCE_GAP"
    if any(r["state"] != "INVALIDATED" and "INPUT_OR_PREFIX_UNAVAILABLE" in r["blockers"]
           for s in sequences for r in s["observations"] if r["within_lifecycle"]):
        return "INSUFFICIENT_FORWARD_EVIDENCE"
    return next(iter(causes)) if causes else "INSUFFICIENT_FORWARD_EVIDENCE"


def _counts(sequences, predicate):
    selected = [s for s in sequences if predicate(s)]
    return {"TOTAL": len(selected),
            **{field: {key: sum(s[field] == key for s in selected) for key in keys}
               for field, keys in (("symbol", capture.SYMBOLS), ("direction", ("LONG", "SHORT")),
                                   ("relationship", ("ALIGNED", "COUNTER_TREND", "NEUTRAL")))}}


def tactical_sequence_acceptance_audit(root=Path("artifact")):
    """Fixed #301-#419 audit. Only immutable inputs/receipts decide acceptance."""
    root = Path(root)
    report = {"schema": "tactical-sequence-acceptance-audit.v1", "read_only": True,
              "origin_creation_allowed": False, "gate_modified": False,
              "cohort": {"first": FIRST_OBSERVATION, "last": LAST_OBSERVATION,
                         "authority": "DURABLE_FORWARD_SELECTION_INPUTS_AND_LEDGER"}}
    try:
        ledger_path = root / capture.LEDGER_NAME
        initial_ledger_hash = capture._sha(ledger_path)
        ledger = capture._read(ledger_path)
        if (ledger.get("schema") != capture.LEDGER_SCHEMA or ledger.get("activation_observation") != FIRST_OBSERVATION
                or ledger.get("origin_creation_allowed") is not False or ledger.get("activation_status") != "SHADOW_ONLY"):
            raise ValueError("INVALID_DURABLE_ACTIVATION")
        records = [r for r in ledger["records"] if FIRST_OBSERVATION <= r["observation_number"] <= LAST_OBSERVATION]
        if [r["observation_number"] for r in records] != list(range(FIRST_OBSERVATION, LAST_OBSERVATION + 1)):
            raise ValueError("INCOMPLETE_DURABLE_COHORT")
        protected_paths = [root / "observation-commit-journal.json", root / "observation-runner.json",
                           root / "production-observation-head.json", root.parent / "reviews/thesis-baseline.json",
                           root.parent / "research/missed-opportunities.json"]
        protected = {p: capture._sha(p) for p in protected_paths if p.exists()}
        current, sequences, input_hashes = {}, {}, {}
        for record in records:
            obs, cp = record["observation_number"], record["checkpoint"]
            path = root / "tactical-shadow-inputs" / f"{obs}.json"
            input_hashes[path] = capture._sha(path)
            bundle = capture._read(path)
            if (capture.digest(bundle) != record["input_sha256"] or bundle.get("capture_version") != capture.CAPTURE_VERSION
                    or bundle.get("status") != "AVAILABLE" or bundle["observation_number"] != obs
                    or bundle["checkpoint"] != cp or set(bundle["symbols"]) != set(capture.SYMBOLS)
                    or set(record["states"]) != set(capture.SYMBOLS) or record.get("production_isolation_verified") is not True):
                raise ValueError("INVALID_DURABLE_RECEIPT")
            for symbol in capture.SYMBOLS:
                snapshot = bundle["symbols"][symbol]
                if snapshot["symbol"] != symbol or snapshot["observation_number"] != obs or snapshot["checkpoint"] != cp:
                    raise ValueError("INVALID_DURABLE_SYMBOL_IDENTITY")
                previous = current.get(symbol)
                state = capture._fold(previous, snapshot)
                if state != record["states"][symbol]:
                    raise ValueError("DURABLE_STATE_REPLAY_MISMATCH")
                inputs = capture.locked_target_inputs(_inputs(snapshot["review"], snapshot["exposure"], cp), snapshot, previous)
                current[symbol] = state
                identity = state["sequence_id"]
                if not identity:
                    continue
                if identity not in sequences:
                    sequences[identity] = {"sequence_id": identity, "symbol": symbol,
                        "direction": state["tactical_direction"], "relationship": state["relationship"],
                        "start_observation": obs, "selected_target_id": state["active_target"]["liquidity_id"],
                        "selected_target": state["active_target"], "target_selection_legal": True,
                        "observations": [], "transitions": []}
                sequence = sequences[identity]
                sequence["observations"].append({"observation": obs, "checkpoint": cp,
                    "state": state["sequence_state"], "relationship": state["relationship"],
                    "observed_tactical_direction": state["observed_tactical_direction"],
                    "direction_evidence": {k: v for k, v in classify_tactical(snapshot["review"], cp).items()
                                           if k in {"supporting_evidence", "missing_evidence"}},
                    "target": state["active_target"], "blockers": state["blockers"],
                    "contextual_mss": state["contextual_mss"], "selected_sweep": state["selected_sweep"],
                    "lifecycle_evaluated": obs > sequence["start_observation"] and
                        state["evaluation_status"] == "AVAILABLE" and state["sequence_state"] != "INVALIDATED"
                        and state["observed_tactical_direction"] == sequence["direction"],
                    "evidence": _probe(inputs, sequence["selected_target"], sequence["direction"])})
                sequence["transitions"].extend(t for t in record["transitions"] if t["sequence_id"] == identity)
            if record["transitions"] != capture._transitions(current, obs, cp):
                raise ValueError("DURABLE_TRANSITION_REPLAY_MISMATCH")
        items = [_summarize_sequence(s) for s in sequences.values()]
        churn = _churn(items)
        invalidations = [s["invalidation"] for s in items if s["invalidation"]]
        reasons = Counter(t["reason"] for t in invalidations)
        categories = {"target_invalidated": 0, "tactical_direction_changed": 0,
                      "new_target_replacement": 0, "primary_relationship_change": 0,
                      "evidence_expiry": 0, "explicit_structural_invalidation": 0, "other": 0}
        for reason, count in reasons.items():
            key = ("tactical_direction_changed" if reason in {"TACTICAL_DIRECTION_WITHDRAWN", "TACTICAL_DIRECTION_REVERSAL"}
                   else "explicit_structural_invalidation" if reason == "LOCKED_SETUP_INVALIDATED" else "other")
            categories[key] += count
        active = {}
        for symbol, state in current.items():
            s = sequences.get(state["sequence_id"])
            if s:
                latest = s["observations"][-1]
                active[symbol] = {k: s[k] for k in ("sequence_id", "direction", "state", "selected_target", "first_blocker")}
                active[symbol].update(relationship=latest["relationship"], current_blocker=latest["blockers"],
                    target_status=s["selected_target"]["status"], target_status_scope="FROZEN_SELECTION_STATUS",
                    post_selection_sweep=s["target_audit"]["post_selection_sweep_during_lifecycle"],
                    matching_displacement=s["funnel"]["MATCHING_DISPLACEMENT_SEEN"],
                    mss_candidate=s["funnel"]["CONTEXTUAL_MSS_CANDIDATE"])
            else:
                active[symbol] = {"state": state["sequence_state"], "current_blocker": state["blockers"]}
        unchanged = (capture._sha(ledger_path) == initial_ledger_hash and
                     all(capture._sha(p) == h for p, h in {**protected, **input_hashes}.items()))
        if not unchanged:
            raise ValueError("SOURCE_CHANGED_DURING_READ_ONLY_AUDIT")
        return {**report, "status": "PASS", "observations": len(records), "sequence_count": len(items),
            "sequences": items, "funnel": {stage: _counts(items, lambda s, st=stage: s["funnel"][st]) for stage in STAGES},
            "first_blockers": dict(Counter(s["first_blocker"] for s in items if s["first_blocker"])),
            "invalidations": {"total": len(invalidations), "reasons": dict(sorted(reasons.items())), "categories": categories},
            "churn": churn, "current": active, "final_assessment": _assessment(items, churn),
            "post_terminal_diagnostics": {
                "targets_with_post_selection_sweep": sum(bool(s["target_audit"]["post_terminal_sweeps"]) for s in items),
                "with_matching_displacement": sum(bool(s["target_audit"]["post_terminal_matching_displacements"]) for s in items),
                "with_mss_candidate": sum(bool(s["target_audit"]["post_terminal_mss_candidates"]) for s in items),
                "eligible_for_lifecycle_progression": False},
            "raw_inventory_diagnostics": {
                "sequences_with_raw_trigger_mss": sum(any(sum(r["evidence"]["raw_inventory_not_linkage"]["trigger_mss"].values())
                    for r in s["observations"] if r["within_lifecycle"]) for s in items),
                "sequences_with_raw_displacement": sum(any(sum(r["evidence"]["raw_inventory_not_linkage"]["displacements"].values())
                    for r in s["observations"] if r["within_lifecycle"]) for s in items),
                "counts_do_not_imply_causal_linkage": True},
            "persisted_mss_confirmed": sum(s["funnel"]["MSS_CONFIRMED"] for s in items),
            "source_hashes_unchanged": unchanged, "deterministic_receipt_replay": "PASS",
            "expected_count_reconciliation": {"expected_starts": 18, "actual_starts": len(items),
                "expected_invalidations": 11, "actual_invalidations": len(invalidations)},
            "limitations": ["Absence means absent in legally frozen detector scope, not proof of no intrabar market interaction.",
                "The existing detector emits the first sweep per prefix/reference; repeated penetrations are not reconstructed.",
                "Post-terminal evidence is shown separately and cannot advance the retired sequence.",
                "Relationships in aggregate are frozen at sequence start; per-observation relationships are retained."]}
    except (OSError, KeyError, TypeError, ValueError) as error:
        allowed = {"INVALID_DURABLE_ACTIVATION", "INCOMPLETE_DURABLE_COHORT", "INVALID_DURABLE_RECEIPT",
                   "INVALID_DURABLE_SYMBOL_IDENTITY", "DURABLE_STATE_REPLAY_MISMATCH",
                   "DURABLE_TRANSITION_REPLAY_MISMATCH", "SOURCE_CHANGED_DURING_READ_ONLY_AUDIT"}
        reason = str(error) if str(error) in allowed else "DURABLE_SOURCE_UNAVAILABLE_OR_INVALID"
        return {**report, "status": "UNAVAILABLE", "reason": reason,
                "final_assessment": "INSUFFICIENT_FORWARD_EVIDENCE"}


def render_acceptance_audit(report):
    if report["status"] != "PASS":
        return f"LIVE COHORT #301-#419: UNAVAILABLE\n{report['reason']}"
    lines = [f"LIVE COHORT #301-#419: {report['sequence_count']} sequences"]
    lines.append("FUNNEL")
    lines.extend(f"{stage}: {counts['TOTAL']}" for stage, counts in report["funnel"].items())
    lines.extend(("FIRST BLOCKERS", json.dumps(report["first_blockers"], sort_keys=True),
                  "INVALIDATIONS", json.dumps(report["invalidations"], sort_keys=True),
                  "SEQUENCE CHURN", json.dumps(report["churn"], sort_keys=True)))
    for s in report["sequences"]:
        lines.append(f"{s['symbol']} {s['sequence_id']} #{s['start_observation']}-#{s['end_observation']} "
                     f"{s['direction']} {s['relationship']} {s['state']} {s['first_blocker']}")
    for symbol, data in report["current"].items():
        lines.extend((f"CURRENT {symbol}", json.dumps(data, sort_keys=True)))
    lines.extend(("FINAL ASSESSMENT", report["final_assessment"]))
    return "\n".join(lines)
