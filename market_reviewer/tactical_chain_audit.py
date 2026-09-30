"""Read-only producer/contract audit, with conditional probes, never a new chain."""
import copy
import json
from collections import Counter
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from . import reviewer as rv
from .live_hybrid_smc import legal_prefix
from .model import TIMEFRAMES, to_market_data_frame
from .research_side_diagnostics import DEFAULT_JOURNAL
from .tactical_direction_shadow import classify_tactical
from .tactical_provenance import (SCHEMA, build_exposure, corrected_ob_provenance_view,
                                  exposure_validation, bind_tactical_identity)

PRODUCERS = {
    "contextual_mss": {
        "path": "reviewer.review_symbol -> _events_for_active_target -> latest_tactical_sweep -> _latest_displacement -> _contextual_mss -> _contextual_mss_text",
        "source_module": "market_reviewer/reviewer.py",
        "timeframes": ["M15", "M5"], "direction_argument": "bias",
        "CONTEXTUAL_MSS_PRIMARY_BIAS_FILTERED": True,
        "selected_displacement_required": True, "selected_active_target_sweep_required": True,
        "selection": "First trigger TF with candidates; last MSS in that TF; direction==bias, MSS timestamp>sweep and >=displacement",
        "production_output": "One selected text; no independent tactical chain enumeration",
        "primary_long_raw_bearish_can_be_hidden": True,
    },
    "active_setup": {
        "path": "reviewer.review_symbol -> _setup_fvg -> _resolve_sequence_lifecycle -> previous setup lock / output_setup_fvg -> _setup_id",
        "source_module": "market_reviewer/reviewer.py", "ACTIVE_SETUP_PRIMARY_SEQUENCE_ONLY": True,
        "zone_types": ["SETUP_FVG"], "OB_or_BPR_or_Breaker_active_id": False,
        "direction_argument": "bias", "production_argument": "Swing_Bias",
        "requires": ["selected sweep", "selected displacement", "selected contextual MSS"],
        "candidate_rule": "FVG direction==bias, formed_at>=sweep/displacement/MSS; last candidate",
        "active_states": ["SETUP_FVG_CREATED", "RETEST_PENDING"],
        "previous_setup": "Preserve refreshed previous active FVG identity while active states continue",
        "opposite_raw_fvg_can_exist_without_active_setup": True,
    },
    "shadow_replay": {
        "helpers_accept_direction": True,
        "reusable_helpers": ["_latest_displacement", "_contextual_mss", "_setup_fvg", "_setup_id"],
        "independent_tactical_target_selection_contract": "MISSING",
        "independent_tactical_active_lifecycle_contract": "MISSING",
        "binding_contract": "bind_tactical_identity requires production Contextual_MSS and Active_Setup_ID",
        "full_independent_active_setup_from_helpers_alone": False,
        "warning": "Conditional producer output does not establish eligible target, origin, or causal authority",
    },
}


def component_trace(review, exposure, checkpoint):
    shadow = classify_tactical(review, checkpoint)
    direction = {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(shadow["tactical_direction"])
    groups = {
        "H1_structure": [e for e in exposure["structure_events"] if e["timeframe"] == "H1"],
        "M15_structure": [e for e in exposure["structure_events"] if e["timeframe"] == "M15"],
        "matching_raw_displacement": [e for e in exposure["raw_directional_displacement"] if e["direction"] == direction],
        "sweep": [e for e in exposure["liquidity_events"] if e["type"] == "SWEPT"],
        "reclaim": [e for e in exposure["liquidity_events"] if e["type"] == "RECLAIMED"],
        "matching_setup_FVG": [e for e in exposure["zones"] if e["type"] == "FVG" and e["direction"] == direction and e["evidence"].get("setup_type") == "SETUP_FVG"],
        "matching_OB": [e for e in exposure["zones"] if e["type"] == "OB" and e["direction"] == direction],
    }
    def safe(e):
        return {k: copy.deepcopy(e.get(k)) for k in ("event_id", "type", "direction", "side", "timeframe",
            "timestamp", "available_at", "setup_id", "pool_id", "related_displacement_id", "related_displacement_event_id")} | {
                "detector_related_displacement_id": e["evidence"].get("related_displacement_id"),
                "detector_context": {k: copy.deepcopy(e["evidence"][k]) for k in
                    ("price", "strength", "structure_broken", "fvg_created", "level_price", "level_type",
                     "upper", "lower", "high", "low", "status") if k in e["evidence"]}}
    h1 = groups["H1_structure"]
    anchors = [e for e in h1 if e["timestamp"] == max(x["timestamp"] for x in h1)] if h1 else []
    return {"primary_direction": shadow["primary_direction"], "tactical_direction": shadow["tactical_direction"],
        "production": {k: review.get(k) for k in ("Swing_Bias", "Contextual_MSS", "Active_Setup_ID", "Active_Tactical_Draw", "Sequence_State")},
        "counts": {k: len(v) for k, v in groups.items()}, "H1_anchors": [safe(e) for e in anchors],
        "components": {k: [safe(e) for e in v] for k, v in groups.items()},
        "component_presence_is_linkage": False}


def trace_components_safely(review, exposure, checkpoint):
    """Inspect components without treating invalid zone identities as ancestry.

    Legacy zone collisions do not invalidate unrelated frozen structures. Each
    zone is still checked for temporal validity, but collision resolution and
    binding remain prohibited unless the entire exposure validates.
    """
    scoped = copy.deepcopy(exposure)
    scoped["zones"] = []
    validation = exposure_validation(scoped, review, checkpoint)
    if not validation["valid"]:
        return {"status": "UNAVAILABLE", "validation": validation}
    rejected = 0
    zones = []
    for zone in exposure.get("zones", []):
        scoped["zones"] = [zone]
        if exposure_validation(scoped, review, checkpoint)["valid"]:
            zones.append(zone)
        else:
            rejected += 1
    scoped["zones"] = zones
    return {"status": "COMPONENTS_ONLY", "identity_binding_authorized": False,
            "zone_counts_unit": "Persisted component records, not unique validated identities",
            "excluded_invalid_zone_records": rejected,
            "trace": component_trace(review, scoped, checkpoint)}


def probe_producers(frames, review, exposure, checkpoint):
    """Conditional invocations for each explicit pool sweep; no target selection.

    This only answers what the unchanged helpers return for supplied inputs.
    None of these invocations is promoted into an independent tactical chain.
    """
    if frames is not None:
        prefix = legal_prefix(frames, review["Symbol"], checkpoint)
        structures = {tf: rv.analyze_structure(prefix[tf]) for tf in TIMEFRAMES}
        ds_by_tf = {tf: rv.find_displacements(prefix[tf], structures[tf]) for tf in TIMEFRAMES}
        displacements = [d for tf in TIMEFRAMES for d in ds_by_tf[tf]]
        fvgs = [g for tf in TIMEFRAMES for g in rv.find_fvgs(prefix[tf], ds_by_tf[tf])]
        source = "EXACT_RAW_PREFIX"
    else:
        # These helpers consume events, not candles. Validate the complete input
        # collections they use; legacy OB collisions are not inputs to either.
        inputs = copy.deepcopy(exposure)
        inputs["zones"] = [z for z in inputs["zones"] if z["type"] == "FVG"]
        if not exposure_validation(inputs, review, checkpoint)["valid"]:
            return {"status": "UNAVAILABLE", "reason": "INVALID_FROZEN_PRODUCER_INPUTS"}
        for collection in ("structure_events", "raw_directional_displacement", "liquidity_events", "zones"):
            for event in inputs[collection]:
                evidence = event["evidence"]
                timestamp_key = "formed_at" if collection == "zones" else "timestamp"
                if (evidence.get(timestamp_key) != event["timestamp"]
                        or ("direction" in evidence and evidence["direction"] != event.get("direction"))
                        or ("timeframe" in evidence and evidence["timeframe"] != event["timeframe"])):
                    return {"status": "UNAVAILABLE", "reason": "FROZEN_PAYLOAD_IDENTITY_MISMATCH"}
        def ordered(collection, tf):
            return sorted((e for e in collection if e["timeframe"] == tf), key=lambda e: e["timestamp"])
        try:
            structures = {tf: SimpleNamespace(events=[rv.StructureEvent(**e["evidence"])
                for e in ordered(inputs["structure_events"], tf)]) for tf in TIMEFRAMES}
            displacements = [rv.DisplacementEvent(**e["evidence"]) for tf in TIMEFRAMES
                for e in ordered(inputs["raw_directional_displacement"], tf)]
            fvgs = [rv.FairValueGap(**e["evidence"]) for tf in TIMEFRAMES for e in ordered(inputs["zones"], tf)]
        except (TypeError, KeyError):
            return {"status": "UNAVAILABLE", "reason": "INCOMPLETE_FROZEN_PRODUCER_INPUTS"}
        source = "FROZEN_EXPOSED_DETECTOR_OUTPUTS_NOT_RAW_REDETECTION"
    tactical = classify_tactical(review, checkpoint)["tactical_direction"]
    direction = {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(tactical)
    trials = []
    if direction:
        side = "SELLSIDE" if direction == "BULLISH" else "BUYSIDE"
        for e in exposure["liquidity_events"]:
            if (e["type"] != "SWEPT" or e.get("side") != side or not e.get("pool_id")
                    or e["timeframe"] not in rv.TACTICAL_TIMEFRAMES):
                continue
            sweep = rv.LiquidityEvent(**e["evidence"])
            displacement = rv._latest_displacement([d for d in displacements if d.timestamp > sweep.timestamp], direction)
            mss = rv._contextual_mss(structures, sweep, displacement, direction)
            setup = rv._setup_fvg(fvgs, sweep, displacement, mss, direction)
            trials.append({"input_sweep_event_id": e["event_id"], "input_pool_id": e["pool_id"],
                "direction_argument": direction, "displacement": asdict(displacement) if displacement else None,
                "contextual_mss": asdict(mss) if mss else None, "candidate_setup_id": rv._setup_id(setup),
                "production_eligible_target_proven": False, "active_setup_authorized": False})
    return {"status": "CONDITIONAL_PRODUCER_PROBE_ONLY", "source": source, "trials": trials,
        "mss_returned": sum(t["contextual_mss"] is not None for t in trials),
        "setup_returned": sum(t["candidate_setup_id"] != "NONE" for t in trials),
        "not_market_absence_proof": True, "no_target_winner_selected": True}


def tactical_chain_audit(journal_path=DEFAULT_JOURNAL):
    result = {"schema": "tactical-chain-producer-audit.v1", "read_only": True,
              "producers": copy.deepcopy(PRODUCERS), "origin_creation_allowed": False}
    try:
        path = Path(journal_path)
        before = path.read_bytes()
        journal = json.loads(before)
        sources, cases, identities = {}, [], {}
        for tx in journal.get("transactions", []):
            payload = tx.get("recovery_payload", {})
            exposures = payload.get("non_canonical_research_evidence", {}).get("tactical_provenance", {})
            if tx.get("status") != "COMPLETE" or tx.get("sample_source") == "HISTORICAL_REPLAY" or not exposures:
                continue
            observation = tx["observation_number"]
            checkpoint = tx["canonical_checkpoint"]
            if payload.get("observation_number") != observation or payload.get("canonical_checkpoint") != checkpoint:
                raise ValueError("SOURCE_IDENTITY_MISMATCH")
            for symbol in ("BTC", "ETH"):
                persisted = exposures.get(symbol)
                if not isinstance(persisted, dict) or persisted.get("schema") != SCHEMA:
                    continue
                review = payload["reviews"][symbol]
                if review.get("Symbol") != symbol or int(review["Review_Timestamp"]) + 300 != checkpoint:
                    raise ValueError("REVIEW_IDENTITY_MISMATCH")
                key = (observation, symbol)
                row_identity = (review, persisted, checkpoint)
                if key in identities:
                    if identities[key] != row_identity:
                        raise ValueError("CONFLICTING_FORWARD_RECORD")
                    continue
                identities[key] = copy.deepcopy(row_identity)
                exposure = persisted
                frames = None
                view = "PERSISTED_VIEW"
                replay_status = "SOURCE_UNAVAILABLE_OR_NOT_REPRODUCIBLE"
                source = Path(str(payload.get("market_path", "")))
                try:
                    if source not in sources:
                        sources[source] = source.read_bytes()
                    raw = json.loads(sources[source])[symbol]
                    candidate_frames = {tf: to_market_data_frame(raw[tf]) for tf in TIMEFRAMES}
                    if all(z.get("ancestry_version") == "ob-displacement-ancestry.v1" for z in persisted.get("zones", []) if z.get("type") == "OB"):
                        rebuilt = build_exposure(candidate_frames, review, checkpoint)
                        if rebuilt != persisted:
                            raise ValueError("SOURCE_CHANGED")
                    else:
                        rebuilt = corrected_ob_provenance_view(candidate_frames, review, checkpoint, persisted)["exposure"]
                        view = "CORRECTED_PROVENANCE_VIEW"
                    exposure, frames, replay_status = rebuilt, candidate_frames, "EXACT_EXPOSURE_REPRODUCED"
                except (OSError, KeyError, ValueError, TypeError):
                    pass
                validation = exposure_validation(exposure, review, checkpoint)
                case = {"observation": observation, "symbol": symbol, "checkpoint": checkpoint, "view": view,
                    "source_replay": replay_status, "validation": validation,
                    "Contextual_MSS_classification": "UNKNOWN", "Active_Setup_classification": "UNKNOWN",
                    "classification_basis": ["Independent tactical active lifecycle absent; production selected fields required",
                                             "Selected NONE alone cannot establish market absence or hidden eligible chain"],
                    "contract_causes": ["E_CONTRACT_REQUIRES_PRODUCTION_SELECTED_FIELD", "B_INDEPENDENT_TACTICAL_SELECTION_NOT_CAPABLE"],
                    "origin_creation_allowed": False}
                components = trace_components_safely(review, exposure, checkpoint)
                case["component_validation"] = {k: v for k, v in components.items() if k != "trace"}
                if "trace" in components:
                    case["trace"] = components["trace"]
                    tactical_bias = {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(case["trace"]["tactical_direction"])
                    if tactical_bias and review.get("Swing_Bias") != tactical_bias:
                        case["contract_causes"].append("C_PRODUCER_PRIMARY_BIAS_FILTERED")
                        case["Contextual_MSS_classification"] = "PRIMARY_BIAS_FILTERED"
                        case["Active_Setup_classification"] = "PRIMARY_BIAS_FILTERED"
                        case["classification_basis"].append("Tactical direction excluded by primary selection; not proof a complete tactical setup exists")
                if validation["valid"]:
                    case["binding"] = bind_tactical_identity(review, exposure, checkpoint)
                else:
                    case["binding"] = {"status": "UNAVAILABLE", "reason": "FULL_EXPOSURE_INVALID", "origin_creation_allowed": False}
                case["probe"] = probe_producers(frames, review, exposure, checkpoint)
                if case["probe"].get("mss_returned", 0):
                    case["classification_basis"].append("Conditional MSS returned; no eligible tactical target selection proven")
                cases.append(case)
        if before != path.read_bytes() or any(b != p.read_bytes() for p, b in sources.items()):
            raise ValueError("SOURCE_CHANGED_DURING_AUDIT")
        cases.sort(key=lambda r: (r["observation"], r["symbol"]))
        result.update(status="PASS", forward_observations=cases,
            baseline_282=[c for c in cases if c["observation"] == 282],
            classification_counts=dict(Counter(c["Contextual_MSS_classification"] for c in cases)),
            assessment="MIXED_CHAIN_GAP" if any("trace" in c for c in cases) else "INSUFFICIENT_EVIDENCE",
            assessment_basis=["Production primary/active-target selection filters evidence", "Binding requires production Contextual_MSS and Active_Setup_ID",
                              "No independent tactical target/lifecycle contract; raw components are not linked ancestry"])
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        result.update(status="UNAVAILABLE", assessment="INSUFFICIENT_EVIDENCE")
    return result


def render_chain_audit(result):
    return "\n".join(title + "\n" + json.dumps(value, indent=2, sort_keys=True) for title, value in (
        ("CONTEXTUAL MSS PRODUCER", result["producers"]["contextual_mss"]),
        ("ACTIVE SETUP PRODUCER", result["producers"]["active_setup"]),
        ("FORWARD OBSERVATIONS", result.get("forward_observations", [])),
        ("#282 TRACE", result.get("baseline_282", [])), ("ASSESSMENT", result["assessment"])))
