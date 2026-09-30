"""Independent, replay-only tactical lifecycle. Never a production or origin gate."""
from __future__ import annotations

import copy
import hashlib
import json
from collections import Counter
from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

from . import reviewer as rv
from .live_hybrid_smc import legal_prefix
from .model import TIMEFRAMES, TIMEFRAME_SECONDS, to_market_data_frame
from .research_side_diagnostics import DEFAULT_JOURNAL
from .tactical_chain_audit import probe_producers
from .tactical_direction_shadow import classify_tactical, relationship
from .tactical_provenance import SCHEMA as EXPOSURE_SCHEMA, exposure_validation, corrected_ob_provenance_view, build_exposure

SCHEMA = "tactical-sequence-shadow.v1"
BIAS = {"LONG": "BULLISH", "SHORT": "BEARISH"}


def _identity(kind, *values):
    raw = json.dumps([SCHEMA, kind, *values], sort_keys=True, separators=(",", ":"), allow_nan=False)
    return "TACTICAL-" + kind + "-" + hashlib.sha256(raw.encode()).hexdigest()[:24]


def _inputs(review, exposure, checkpoint):
    """Only collections consumed by these helpers; OB is never setup authority."""
    scoped = copy.deepcopy(exposure)
    scoped["zones"] = [z for z in scoped["zones"] if z["type"] == "FVG"]
    if not exposure_validation(scoped, review, checkpoint)["valid"]:
        raise ValueError("INVALID_FROZEN_INPUTS")
    for key in ("structure_events", "raw_directional_displacement", "liquidity_events", "zones"):
        for e in scoped[key]:
            data = e["evidence"]
            stamp = "formed_at" if key == "zones" else "timestamp"
            if (data.get(stamp) != e["timestamp"]
                    or ("direction" in data and data["direction"] != e.get("direction"))
                    or ("timeframe" in data and data["timeframe"] != e["timeframe"])):
                raise ValueError("FROZEN_PAYLOAD_IDENTITY_MISMATCH")
            expected_kind = (data.get("kind") if key == "structure_events" else
                             data.get("event_type") if key == "liquidity_events" else
                             "DISPLACEMENT" if key == "raw_directional_displacement" else "FVG")
            if e["type"] != expected_kind:
                raise ValueError("FROZEN_EVENT_TYPE_MISMATCH")
            event_type = {"structure_events": rv.StructureEvent,
                          "raw_directional_displacement": rv.DisplacementEvent,
                          "liquidity_events": rv.LiquidityEvent, "zones": rv.FairValueGap}[key]
            event_type(**data)
    def ordered(key):
        return sorted(scoped[key], key=lambda e: (TIMEFRAMES.index(e["timeframe"]), e["timestamp"], e["event_id"]))
    scoped = {**scoped, **{k: ordered(k) for k in ("structure_events", "raw_directional_displacement", "zones")}}
    return scoped


def _verified_prefix(frames, review, exposure, checkpoint):
    if frames is None:
        return None
    if frames["M5"].latest_closed_candle_timestamp + 300 != checkpoint:
        raise ValueError("SOURCE_BOUNDARY_MISMATCH")
    if all(z.get("ancestry_version") == "ob-displacement-ancestry.v1"
           for z in exposure["zones"] if z["type"] == "OB"):
        if build_exposure(frames, review, checkpoint) != exposure:
            raise ValueError("SOURCE_PARITY_MISMATCH")
    else:
        corrected_ob_provenance_view(frames, review, checkpoint, exposure)
    return legal_prefix(frames, review["Symbol"], checkpoint)


def select_target(review, prefix, direction, checkpoint, retired):
    """Reuse production ranking, prove winner was exposed; never parse draw text."""
    if prefix is None:
        return None, "TACTICAL_ACTIVE_TARGET_UNAVAILABLE:EXACT_PRICE_AND_SELECTION_SCOPE_MISSING"
    levels = [l for tf in TIMEFRAMES for l in rv.find_liquidity(prefix[tf], rv.analyze_structure(prefix[tf]))]
    exposed = review.get("Liquidity")
    if not isinstance(exposed, list) or exposed != [asdict(l) for l in levels[-24:]]:
        return None, "TACTICAL_ACTIVE_TARGET_UNAVAILABLE:LIQUIDITY_SCOPE_PARITY_MISMATCH"
    # No primary sequence state is supplied. Only the existing ranking is reused.
    _, _, candidate = rv._liquidity_draws(BIAS[direction], review.get("Current_Phase"), levels, rv._current_price(prefix))
    if candidate is None or asdict(candidate) not in exposed:
        return None, "TACTICAL_ACTIVE_TARGET_UNAVAILABLE:WINNER_NOT_EXPOSED"
    if candidate.timeframe not in rv.TACTICAL_TIMEFRAMES or not candidate.liquidity_id:
        return None, "TACTICAL_ACTIVE_TARGET_UNAVAILABLE:TACTICAL_IDENTITY_MISSING"
    if candidate.liquidity_id in retired:
        return None, "TACTICAL_ACTIVE_TARGET_UNAVAILABLE:RETIRED_SHADOW_TARGET"
    if candidate.formed_at + TIMEFRAME_SECONDS[candidate.timeframe] > checkpoint:
        return None, "TACTICAL_ACTIVE_TARGET_UNAVAILABLE:FUTURE_REFERENCE"
    return {**asdict(candidate), "side": "SELLSIDE" if direction == "LONG" else "BUYSIDE",
            "timestamp": candidate.formed_at, "available_at": checkpoint, "selected_at": checkpoint,
            "source_module": "market_reviewer.reviewer._liquidity_draws",
            "evidence_scope": "EXACT_LEGAL_PREFIX_WINNER_PRESENT_IN_REVIEW_LIQUIDITY"}, None


def _level(target):
    return rv.LiquidityLevel(**{k: target[k] for k in rv.LiquidityLevel.__dataclass_fields__})


def _transition(state, new, checkpoint, evidence):
    if state["sequence_state"] == new:
        return
    state["transitions"].append({"previous_state": state["sequence_state"], "new_state": new,
        "checkpoint": checkpoint, "evidence": copy.deepcopy(evidence)})
    state["sequence_state"] = new


def _invalidate(state, checkpoint, reason):
    _transition(state, "INVALIDATED", checkpoint, {"reason": reason})
    target = state.get("active_target")
    if target:
        state["retired_target_ids"] = sorted(set(state["retired_target_ids"]) | {target["liquidity_id"]})
    state["blockers"] = [reason]


def _base(review, checkpoint, observation, direction):
    return {"schema": SCHEMA, "symbol": review["Symbol"], "checkpoint": checkpoint,
        "observation": observation, "tactical_direction": direction["tactical_direction"],
        "observed_tactical_direction": direction["tactical_direction"],
        "primary_direction": direction["primary_direction"], "relationship": direction["relationship"],
        "sequence_id": None, "sequence_state": "NONE", "active_target": None,
        "contextual_mss": None, "selected_displacement": None, "selected_sweep": None,
        "active_setup_id": None, "active_setup": None, "eligible_retest": {"confirmed": False},
        "transitions": [], "blockers": [], "retired_target_ids": [],
        "activation_status": "SHADOW_ONLY", "persistence_scope": "NON_CANONICAL",
        "canonical": False, "origin_creation_allowed": False}


def advance(previous, review, exposure, checkpoint, observation, frames=None):
    """Pure checkpoint fold. Returns a new state, preserving all supplied objects.

    A target is frozen before subsequent sweep evidence can advance it. Transition
    checkpoints record when evidence was first evaluated, never a retroactive
    event time. Missing evidence suspends evaluation without erasing the lifecycle.
    """
    fingerprint = _identity("INPUT", review, exposure, checkpoint, observation)
    if previous:
        if previous.get("schema") != SCHEMA or previous["symbol"] != review["Symbol"]:
            raise ValueError("SHADOW_STATE_IDENTITY_MISMATCH")
        if checkpoint < previous["checkpoint"]:
            raise ValueError("BACKWARD_CHECKPOINT")
        if checkpoint == previous["checkpoint"]:
            if previous.get("input_id") != fingerprint:
                raise ValueError("CONFLICTING_CHECKPOINT")
            return copy.deepcopy(previous)
        if observation <= previous["observation"]:
            raise ValueError("BACKWARD_OBSERVATION")
    direction = classify_tactical(review, checkpoint)
    state = copy.deepcopy(previous) if previous else _base(review, checkpoint, observation, direction)
    if not state["sequence_id"]:
        state["tactical_direction"] = direction["tactical_direction"]
    state.update(checkpoint=checkpoint, observation=observation, input_id=fingerprint,
                 primary_direction=direction["primary_direction"],
                 observed_tactical_direction=direction["tactical_direction"],
                 relationship=relationship(direction["primary_direction"], state["tactical_direction"]),
                 evaluation_status="AVAILABLE", blockers=[])
    tactical = direction["tactical_direction"]
    try:
        if int(review["Review_Timestamp"]) + 300 != checkpoint:
            raise ValueError("REVIEW_BOUNDARY_MISMATCH")
        inputs = _inputs(review, exposure, checkpoint)
        prefix = _verified_prefix(frames, review, exposure, checkpoint)
    except (KeyError, TypeError, ValueError):
        state["evaluation_status"] = "UNAVAILABLE"
        state["blockers"] = ["INPUT_OR_PREFIX_UNAVAILABLE"]
        if not state["sequence_id"]:
            state["sequence_state"] = "UNAVAILABLE"
        return state
    if tactical not in BIAS:
        if state["sequence_id"] and state["sequence_state"] != "INVALIDATED" and tactical == "NONE":
            _invalidate(state, checkpoint, "TACTICAL_DIRECTION_WITHDRAWN")
        else:
            state["blockers"] = ["EXPLICIT_TACTICAL_DIRECTION_UNAVAILABLE"]
        if not state["sequence_id"]:
            state.update(tactical_direction=tactical, sequence_state="NONE" if tactical == "NONE" else "UNAVAILABLE")
        return state
    if state["sequence_id"] and state["sequence_state"] != "INVALIDATED" and tactical != state["tactical_direction"]:
        _invalidate(state, checkpoint, "TACTICAL_DIRECTION_REVERSAL")
        return state
    if not state["sequence_id"] or state["sequence_state"] == "INVALIDATED":
        target, reason = select_target(review, prefix, tactical, checkpoint, state["retired_target_ids"])
        if target is None:
            state["blockers"] = [reason]
            if not state["sequence_id"]:
                state.update(sequence_state="UNAVAILABLE", tactical_direction=tactical)
            return state
        retired = state["retired_target_ids"]
        state = _base(review, checkpoint, observation, direction)
        state.update(input_id=fingerprint, evaluation_status="AVAILABLE", active_target=target,
            retired_target_ids=retired, started_at=checkpoint,
            sequence_id=_identity("SEQUENCE", review["Symbol"], tactical, checkpoint, target["liquidity_id"]))
        _transition(state, "FORMING", checkpoint, {"target_id": target["liquidity_id"]})
        state["blockers"] = ["AWAITING_POST_SELECTION_SWEEP"]
        return state

    # Active identities are locked. No candidate FVG or nearest draw may replace them.
    if state["active_setup_id"]:
        return _advance_retest(state, inputs, prefix, checkpoint)
    target = state["active_target"]
    events = [e for e in inputs["liquidity_events"] if e.get("pool_id") == target["liquidity_id"]]
    selected_events = rv._events_for_active_target([rv.LiquidityEvent(**e["evidence"]) for e in events],
                                                   _level(target), target["selected_at"])
    sweeps = [e for e in events if e["type"] == "SWEPT" and rv.LiquidityEvent(**e["evidence"]) in selected_events]
    if not state["contextual_mss"]:
        if not sweeps:
            state["blockers"] = ["MATCHING_POST_SELECTION_SWEEP_MISSING"]
            return state
        last = max(e["timestamp"] for e in sweeps)
        sweeps = [e for e in sweeps if e["timestamp"] == last]
        if len(sweeps) != 1:
            state["blockers"] = ["SWEEP_IDENTITY_AMBIGUOUS"]
            return state
        sweep = sweeps[0]
        ds = [e for e in inputs["raw_directional_displacement"] if e["timestamp"] > sweep["timestamp"]]
        displacement = rv._latest_displacement([rv.DisplacementEvent(**e["evidence"]) for e in ds], BIAS[tactical])
        matches = [e for e in ds if displacement and e["evidence"] == asdict(displacement)]
        if len(matches) != 1:
            state["blockers"] = ["DISPLACEMENT_LINKAGE_MISSING_OR_AMBIGUOUS"]
            return state
        structures = {tf: SimpleNamespace(events=[rv.StructureEvent(**e["evidence"])
            for e in inputs["structure_events"] if e["timeframe"] == tf]) for tf in TIMEFRAMES}
        mss = rv._contextual_mss(structures, rv.LiquidityEvent(**sweep["evidence"]), displacement, BIAS[tactical])
        if mss is None:
            state["blockers"] = ["CONTEXTUAL_MSS_MISSING"]
            return state
        # Resolve the producer's TF priority without inventing a cross-TF link.
        anchors = []
        for tf in rv.TRIGGER_TIMEFRAMES:
            anchors = [e for e in inputs["structure_events"] if e["timeframe"] == tf and e["type"] == "MSS"
                       and e["timestamp"] == mss.timestamp and e["direction"] == mss.direction
                       and e["evidence"].get("price") == mss.price]
            if anchors:
                break
        if len(anchors) != 1:
            state["blockers"] = ["CONTEXTUAL_MSS_IDENTITY_AMBIGUOUS"]
            return state
        state["selected_sweep"] = copy.deepcopy(sweep)
        state["selected_displacement"] = copy.deepcopy(matches[0])
        state["contextual_mss"] = {**asdict(mss), "trigger_timeframe": anchors[0]["timeframe"],
            "event_id": anchors[0]["event_id"], "available_at": checkpoint,
            "related_sweep_id": sweep["event_id"], "related_displacement_id": matches[0]["event_id"]}
        _transition(state, "MSS_CONFIRMED", checkpoint, state["contextual_mss"])

    sweep = rv.LiquidityEvent(**state["selected_sweep"]["evidence"])
    displacement = rv.DisplacementEvent(**state["selected_displacement"]["evidence"])
    mss = rv.BreakEvent(**{k: state["contextual_mss"][k] for k in rv.BreakEvent.__dataclass_fields__})
    gap = rv._setup_fvg([rv.FairValueGap(**e["evidence"]) for e in inputs["zones"]], sweep, displacement, mss, BIAS[tactical])
    if gap is None:
        state["blockers"] = ["FVG_ELIGIBILITY_MISSING"]
        return state
    parent = state["selected_displacement"]
    if gap.related_displacement_id != f"{parent['timeframe']}:{parent['timestamp']}":
        state["blockers"] = ["FVG_DISPLACEMENT_LINK_MISMATCH"]
        return state
    if gap.status == "INVALIDATED":
        state["blockers"] = ["SETUP_CANDIDATE_ALREADY_INVALIDATED"]
        return state
    zone_matches = [e for e in inputs["zones"] if e["evidence"] == asdict(gap)]
    if len(zone_matches) != 1:
        state["blockers"] = ["SETUP_IDENTITY_AMBIGUOUS"]
        return state
    state["active_setup"] = asdict(gap)
    state["setup_selected_at"] = checkpoint
    state["active_setup_id"] = _identity("SETUP", state["symbol"], tactical, rv._setup_id(gap),
        parent["event_id"], state["contextual_mss"]["event_id"], state["sequence_id"])
    state["detector_setup_id"] = rv._setup_id(gap)
    state["setup_event_id"] = zone_matches[0]["event_id"]
    _transition(state, "SETUP_FORMED", checkpoint, {"setup_id": state["active_setup_id"]})
    # Do not count a historical interaction or same-checkpoint candle as a retest.
    state["blockers"] = ["AWAITING_FORWARD_RETEST_EVALUATION"]
    return state


def _advance_retest(state, inputs, prefix, checkpoint):
    gap = rv.FairValueGap(**state["active_setup"])
    same = [e for e in inputs["zones"] if e["event_id"] == state["setup_event_id"]
            and e.get("setup_id") == state["detector_setup_id"]
            and e["evidence"].get("related_displacement_id") == gap.related_displacement_id
            and e["evidence"].get("upper") == gap.upper and e["evidence"].get("lower") == gap.lower]
    if any(e["evidence"].get("status") == "INVALIDATED" for e in same):
        _invalidate(state, checkpoint, "LOCKED_SETUP_INVALIDATED")
        return state
    if state["sequence_state"] == "SETUP_FORMED":
        _transition(state, "RETEST_PENDING", checkpoint, {"setup_id": state["active_setup_id"]})
    if prefix is None:
        state["evaluation_status"] = "UNAVAILABLE"
        state["blockers"] = ["RETEST_CLOSED_CANDLE_SCOPE_UNAVAILABLE"]
        return state
    frame = prefix[gap.timeframe]
    seconds = TIMEFRAME_SECONDS[gap.timeframe]
    candles = frame.closed_candles()
    needed = range(gap.formed_at + seconds, frame.latest_closed_candle_timestamp + seconds, seconds)
    if not set(needed).issubset({c.timestamp for c in candles}):
        state["evaluation_status"] = "UNAVAILABLE"
        state["blockers"] = ["RETEST_HISTORY_GAP"]
        return state
    refreshed = rv._refresh_setup_status(prefix, gap)
    if refreshed.status == "INVALIDATED":
        _invalidate(state, checkpoint, "LOCKED_SETUP_INVALIDATED")
        return state
    state["active_setup"] = asdict(refreshed)
    forward = [c for c in candles if c.timestamp >= state["setup_selected_at"]]
    scoped = {**prefix, gap.timeframe: replace(frame, candles=forward)}
    retest = rv._eligible_setup_retest(scoped, refreshed)
    if retest.confirmed and retest.setup_id == state["detector_setup_id"]:
        state["eligible_retest"] = {**asdict(retest), "setup_id": state["active_setup_id"],
                                    "detector_setup_id": retest.setup_id, "available_at": checkpoint}
        _transition(state, "RETEST_CONFIRMED", checkpoint, state["eligible_retest"])
        state["blockers"] = []
    else:
        state["blockers"] = [] if state["eligible_retest"]["confirmed"] else ["NO_FORWARD_ELIGIBLE_RETEST"]
    return state


def trace_288(review, exposure, checkpoint, state):
    """Probe diagnostics are not inputs to the lifecycle or target selector."""
    probe = probe_producers(None, review, exposure, checkpoint)
    trials = []
    for trial in probe.get("trials", []):
        if trial["contextual_mss"] is None:
            continue
        sweep = next(e for e in exposure["liquidity_events"] if e["event_id"] == trial["input_sweep_event_id"])
        displacement_ids = [{"event_id": e["event_id"], "timeframe": e["timeframe"], "timestamp": e["timestamp"]}
                            for e in exposure["raw_directional_displacement"] if e["evidence"] == trial["displacement"]]
        mss_ids = [{"event_id": e["event_id"], "timeframe": e["timeframe"], "timestamp": e["timestamp"]}
                   for e in exposure["structure_events"] if e["type"] == "MSS"
                   and e["timestamp"] == trial["contextual_mss"]["timestamp"]
                   and e["direction"] == trial["direction_argument"]]
        cutoff = max(trial["contextual_mss"]["timestamp"], trial["displacement"]["timestamp"])
        counts = Counter()
        latest_setup = None
        for z in exposure["zones"]:
            if z["type"] != "FVG":
                continue
            gap = z["evidence"]
            if gap["direction"] != trial["direction_argument"]:
                counts["OPPOSITE_DIRECTION"] += 1
            elif gap["setup_type"] != "SETUP_FVG":
                counts["GENERIC_NOT_SETUP_FVG"] += 1
            elif gap["formed_at"] < cutoff:
                counts["FORMED_BEFORE_MSS_OR_DISPLACEMENT"] += 1
                latest_setup = max(latest_setup or 0, gap["formed_at"])
            else:
                counts["PASSES_EXISTING_SETUP_FILTER"] += 1
        trials.append({**trial, "required_fvg_formed_at_min": cutoff,
                       "sweep_timestamp": sweep["timestamp"], "sweep_timeframe": sweep["timeframe"],
                       "swept_reference_type": sweep["evidence"]["level_type"],
                       "displacement_candidates": displacement_ids, "mss_candidates": mss_ids,
                       "latest_earlier_matching_setup_fvg": latest_setup,
                       "fvg_filter_counts": dict(sorted(counts.items()))})
    return {"tactical_direction": state["tactical_direction"],
        "primary_direction": state["primary_direction"], "conditional_mss_candidates": trials,
        "conditional_mss_count": probe.get("mss_returned"), "conditional_setup_count": probe.get("setup_returned"),
        "candidate_does_not_authorize_target": True, "active_target": state["active_target"],
        "target_selection_contract": "_liquidity_draws uses Sell-side / Buy-side typed references; Equal Lows / Equal Highs probes do not satisfy that type filter",
        "shadow_contextual_mss": state["contextual_mss"], "sequence_state": state["sequence_state"],
        "blockers": state["blockers"], "can_establish_SETUP_FORMED": state["active_setup_id"] is not None}


def _metrics(rows):
    states = ("FORMING", "MSS_CONFIRMED", "SETUP_FORMED", "RETEST_PENDING", "RETEST_CONFIRMED", "INVALIDATED")
    metrics = {"forward_observations": len(rows), "count_unit": "symbol_checkpoint_rows",
               "distinct_observations": len({r["observation"] for r in rows})}
    for name in states:
        seen = {(r["sequence_id"], t["checkpoint"]) for r in rows for t in r["transitions"]
                if t["new_state"] == name and t["checkpoint"] == r["checkpoint"]}
        metrics[name] = {"count": len(seen), "observations": sorted({r["observation"] for r in rows
            if any(t["new_state"] == name and t["checkpoint"] == r["checkpoint"] for t in r["transitions"])})}
    metrics["sequence_started"] = metrics["FORMING"]
    metrics["blockers"] = dict(sorted(Counter(b for r in rows for b in r["blockers"]).items()))
    return metrics


def tactical_sequence_shadow(journal_path=DEFAULT_JOURNAL):
    """Deterministic replay in memory; no shadow or canonical file is written."""
    result = {"schema": "tactical-sequence-shadow-report.v1", "read_only": True,
        "activation_status": "SHADOW_ONLY", "origin_creation_allowed": False,
        "recommendation": "KEEP_SHADOW_SEQUENCE_EXPERIMENTAL", "persistence": "IN_MEMORY_REPLAY_ONLY"}
    try:
        path = Path(journal_path)
        before = path.read_bytes()
        journal = json.loads(before)
        items = {}
        for tx in journal.get("transactions", []):
            if tx.get("status") != "COMPLETE" or tx.get("sample_source") == "HISTORICAL_REPLAY":
                continue
            p = tx.get("recovery_payload", {})
            exposures = p.get("non_canonical_research_evidence", {}).get("tactical_provenance", {})
            if not exposures:
                continue
            obs, cp = tx["observation_number"], tx["canonical_checkpoint"]
            if obs < 282:
                continue
            if p.get("observation_number") != obs or p.get("canonical_checkpoint") != cp:
                raise ValueError("SOURCE_IDENTITY_MISMATCH")
            for symbol in ("BTC", "ETH"):
                e = exposures.get(symbol)
                if not isinstance(e, dict) or e.get("schema") != EXPOSURE_SCHEMA:
                    continue
                row = (p["reviews"][symbol], e, cp, p.get("market_path"))
                key = (obs, symbol)
                if key in items and items[key] != row:
                    raise ValueError("CONFLICTING_FORWARD_RECORD")
                if row[0].get("Symbol") != symbol:
                    raise ValueError("SYMBOL_MISMATCH")
                items[key] = row
        latest, rows, sources, raw_cache = {}, [], {}, {}
        for (obs, symbol), (review, exposure, cp, market_path) in sorted(items.items()):
            frames = None
            source_status = "EXACT_SOURCE_UNAVAILABLE"
            if market_path:
                source = Path(market_path)
                try:
                    if source not in sources:
                        sources[source] = source.read_bytes()
                        raw_cache[source] = json.loads(sources[source])
                    raw = raw_cache[source][symbol]
                    if raw["M5"]["latest_closed_candle_timestamp"] + 300 == cp:
                        frames = {tf: to_market_data_frame(raw[tf]) for tf in TIMEFRAMES}
                        source_status = "BOUNDARY_MATCH_REQUIRES_PARITY"
                except (OSError, KeyError, TypeError, ValueError):
                    frames = None
            state = advance(latest.get(symbol), review, exposure, cp, obs, frames)
            latest[symbol] = state
            rows.append(copy.deepcopy(state))
            rows[-1]["source_status"] = source_status
            if obs == 288 and symbol == "ETH":
                result["ETH_288_trace"] = trace_288(review, exposure, cp, state)
        if before != path.read_bytes() or any(p.read_bytes() != data for p, data in sources.items()):
            raise ValueError("SOURCE_CHANGED_DURING_REPLAY")
        result.update(status="PASS", forward_observations=rows, metrics=_metrics(rows),
            latest_states=latest, by_symbol={s: _metrics([r for r in rows if r["symbol"] == s]) for s in ("BTC", "ETH")},
            by_direction={d: _metrics([r for r in rows if r["tactical_direction"] == d]) for d in ("LONG", "SHORT")},
            by_context={f"PRIMARY_{p}_TACTICAL_{d}": _metrics([r for r in rows if r["primary_direction"] == p and r["tactical_direction"] == d])
                for p, d in (("LONG", "SHORT"), ("SHORT", "LONG"), ("NONE", "LONG"), ("NONE", "SHORT"), ("LONG", "LONG"), ("SHORT", "SHORT"))})
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        result.update(status="UNAVAILABLE", reason="SOURCE_OR_REPLAY_VALIDATION_FAILED")
    return result


def render_shadow_sequence(result):
    return "SHADOW TACTICAL SEQUENCE\n" + json.dumps(result, indent=2, sort_keys=True)
