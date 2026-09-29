"""Non-canonical raw exposure and conservative identity binding; never a gate."""
from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import asdict

from . import reviewer as rv
from .live_hybrid_smc import legal_prefix
from .model import TIMEFRAME_SECONDS
from .tactical_direction_shadow import _breaks, classify_tactical

SCHEMA = "tactical-evidence-provenance.v1"


def _canonical_payload(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def deduplicate_zones(exposure):
    """Private corrected view: only entire, identical-ID payloads may collapse.

    Different payloads under one ID are all retained, including any identical
    copies within the colliding group. Identity collisions must remain visible.
    """
    result = copy.deepcopy(exposure)
    groups = {}
    for zone in result["zones"]:
        groups.setdefault(zone["event_id"], []).append(zone)
    retained, removed, collisions, duplicate_groups = [], 0, 0, 0
    for key in sorted(groups):
        zones = groups[key]
        normalized = {_canonical_payload(z) for z in zones}
        if len(zones) > 1:
            duplicate_groups += 1
        if len(normalized) > 1:
            collisions += 1
            retained.extend(zones)
        else:
            removed += len(zones) - 1
            retained.append(zones[0])
    retained.sort(key=lambda z: (z["timestamp"], z["timeframe"], z["event_id"], _canonical_payload(z)))
    result["zones"] = retained
    result["zone_identity_diagnostics"] = {
        "duplicate_group_count": duplicate_groups, "zone_deduplicated_count": removed,
        "collision_group_count": collisions,
        "reason_codes": (["DUPLICATE_IDENTICAL_ZONE_DEDUPED"] if removed else []) +
                        (["EVENT_ID_COLLISION_DIFFERENT_PAYLOAD"] if collisions else []),
    }
    return result


def identity(kind, *parts):
    data = json.dumps([SCHEMA, kind, *parts], sort_keys=True, separators=(",", ":"))
    return kind + "-" + hashlib.sha256(data.encode()).hexdigest()[:24]


def order_blocks_with_ancestry(frame, structure, displacements):
    """Use the actual detector per cause, then verify its existing tail selection.

    Each tuple obtains its parent from the detector invocation, never a positional
    join, strength comparison, nearest timestamp, or inferred candle proximity.
    """
    pairs = []
    for displacement in displacements:
        pairs.extend((block, displacement) for block in
                     rv.find_order_blocks(frame, structure, [displacement]))
    retained = pairs[-8:]
    if [block for block, _ in retained] != rv.find_order_blocks(frame, structure, displacements):
        raise ValueError("OB_DETECTOR_ANCESTRY_PARITY_MISMATCH")
    return retained


def build_exposure(frames, review, checkpoint):
    """Re-run unchanged detectors over the legal prefix; no selected review edits.

    available_at is a conservative checkpoint upper bound, not event-close time:
    displacement follow-through and zone status can depend on later closed bars.
    """
    symbol = review["Symbol"]
    if int(review["Review_Timestamp"]) + 300 != checkpoint:
        raise ValueError("REVIEW_BOUNDARY_MISMATCH")
    prefix = legal_prefix(frames, symbol, checkpoint)
    if prefix["M5"].latest_closed_candle_timestamp + 300 != checkpoint:
        raise ValueError("PREFIX_BOUNDARY_MISMATCH")
    result = {"schema": SCHEMA, "symbol": symbol, "checkpoint": checkpoint,
        "status": "AVAILABLE", "canonical": False, "origin_creation_allowed": False,
        "production_selected_displacement": review.get("Displacement", "UNAVAILABLE"),
        "primary_bias_at_selection": review.get("Swing_Bias", "UNAVAILABLE"),
        "raw_directional_displacement": [], "structure_events": [], "liquidity_events": [],
        "zones": [], "scope": "DETECTOR_OUTPUT_OVER_LEGAL_INPUT_PREFIX",
        "limits": "Existing liquidity selection, FVG last16/TF and OB last8/TF retained; not entire market history"}

    def expose(tf, kind, ts, evidence, anchor=None):
        return {"event_id": identity(kind, symbol, tf, ts, evidence.get("direction"), anchor or evidence),
            "symbol": symbol, "timeframe": tf, "type": kind, "timestamp": ts,
            "closed_candle_timestamp": ts, "candle_close_timestamp": ts + TIMEFRAME_SECONDS[tf],
            "source_candle_id": f"{symbol}:{tf}:{ts}", "available_at": checkpoint,
            "availability_provenance": "CHECKPOINT_VERIFIED_UPPER_BOUND",
            "producer": "market_reviewer.reviewer", "evidence": evidence}

    for tf, frame in prefix.items():
        structure = rv.analyze_structure(frame)
        displacements = rv.find_displacements(frame, structure)
        for d in displacements:
            item = expose(tf, "DISPLACEMENT", d.timestamp, asdict(d), {"direction": d.direction})
            item.update(direction=d.direction, selected_for_production=False,
                primary_bias_at_selection=review.get("Swing_Bias", "UNAVAILABLE"),
                selection_reason="NOT_MATCHED_TO_PRODUCTION_SELECTED_TEXT",
                producer="market_reviewer.reviewer.find_displacements")
            if rv._displacement_text(d) == review.get("Displacement"):
                item["selection_reason"] = "SELECTED_TEXT_MATCH"
            result["raw_directional_displacement"].append(item)
        for e in structure.events:
            item = expose(tf, e.kind, e.timestamp, asdict(e), {"price": e.price, "direction": e.direction})
            item.update(direction=e.direction, producer="market_reviewer.reviewer.analyze_structure",
                        relation_to_H1="UNAVAILABLE")
            result["structure_events"].append(item)
        levels = rv.find_liquidity(frame, structure)
        for e in rv.find_liquidity_events(frame, levels):
            candidates = [l for l in levels if l.price == e.level_price and l.type == e.level_type]
            pool_ids = sorted({l.liquidity_id for l in candidates if l.liquidity_id})
            item = expose(tf, e.event_type, e.timestamp, asdict(e))
            item.update(side="BUYSIDE" if "Buy-side" in e.level_type or e.level_type == "Equal Highs" else
                        "SELLSIDE" if "Sell-side" in e.level_type or e.level_type == "Equal Lows" else "UNAVAILABLE",
                        pool_id=pool_ids[0] if len(pool_ids) == 1 else None,
                        pool_identity_state="EXACT" if len(pool_ids) == 1 else "UNAVAILABLE",
                        producer="market_reviewer.reviewer.find_liquidity_events")
            result["liquidity_events"].append(item)
        for kind, zones in (("FVG", [(z, None) for z in rv.find_fvgs(frame, displacements)]),
                            ("OB", order_blocks_with_ancestry(frame, structure, displacements))):
            for z, cause in zones:
                data = asdict(z)
                anchor = {k: data[k] for k in ("direction", "upper", "lower", "high", "low") if k in data}
                if cause is not None:
                    parent_id = identity("DISPLACEMENT", symbol, tf, cause.timestamp, cause.direction,
                                         {"direction": cause.direction})
                    anchor["related_displacement_event_id"] = parent_id
                item = expose(tf, kind, z.formed_at, data,
                              anchor)
                item.update(direction=z.direction, setup_id=rv._setup_id(z) if kind == "FVG" and z.setup_type == "SETUP_FVG" else None)
                if cause is not None:
                    item.update(ancestry_version="ob-displacement-ancestry.v1",
                        related_displacement_timestamp=cause.timestamp,
                        related_displacement_direction=cause.direction,
                        related_displacement_id=f"{tf}:{cause.timestamp}",
                        related_displacement_event_id=parent_id,
                        related_displacement_strength=cause.strength,
                        related_displacement_body_ratio=cause.body_ratio,
                        ancestry_producer="market_reviewer.reviewer.find_order_blocks")
                result["zones"].append(item)
    matches = [d for d in result["raw_directional_displacement"] if d["selection_reason"] == "SELECTED_TEXT_MATCH"]
    for d in matches:
        d["selected_for_production"] = len(matches) == 1
        d["selection_reason"] = "EXACT_SELECTED_TEXT_MATCH" if len(matches) == 1 else "SELECTION_TIMEFRAME_AMBIGUOUS"
    for key in ("raw_directional_displacement", "structure_events", "liquidity_events", "zones"):
        result[key].sort(key=lambda e: (e["timestamp"], e["timeframe"], e["event_id"]))
    return deduplicate_zones(result)


def capture_noncanonical(frames_by_symbol, reviews, checkpoint):
    """Safe forward-only payload area; failures never affect production success."""
    captured = {}
    for symbol in ("BTC", "ETH"):
        if symbol not in reviews:
            continue
        try:
            captured[symbol] = build_exposure(frames_by_symbol[symbol], reviews[symbol], checkpoint)
        except Exception:
            captured[symbol] = {"schema": SCHEMA, "symbol": symbol, "checkpoint": checkpoint,
                                "status": "UNAVAILABLE", "reason": "EXPOSURE_FAILED", "canonical": False}
    return {"schema": "non-canonical-tactical-research.v1", "tactical_provenance": captured}


def corrected_ob_provenance_view(frames, review, checkpoint, persisted):
    """Explicit read-only replay, rejected unless old detector payloads reproduce.

    This is not called by runtime recovery or status: old records stay untouched.
    Compare full legacy zone payloads with multiplicity before applying new IDs.
    """
    corrected = build_exposure(frames, review, checkpoint)
    for key, value in persisted.items():
        if key not in ("zones", "zone_identity_diagnostics") and corrected.get(key) != value:
            raise ValueError("PERSISTED_SOURCE_REPLAY_MISMATCH")
    legacy_zones = copy.deepcopy(corrected["zones"])
    for zone in legacy_zones:
        if zone["type"] != "OB":
            continue
        for key in list(zone):
            if key.startswith("related_displacement_") or key in ("ancestry_version", "ancestry_producer"):
                zone.pop(key)
        evidence = zone["evidence"]
        anchor = {k: evidence[k] for k in ("direction", "upper", "lower", "high", "low") if k in evidence}
        zone["event_id"] = identity("OB", zone["symbol"], zone["timeframe"], zone["timestamp"], zone["direction"], anchor)
    if sorted(map(_canonical_payload, legacy_zones)) != sorted(map(_canonical_payload, persisted["zones"])):
        raise ValueError("PERSISTED_OB_REPLAY_MISMATCH")
    return {"view": "CORRECTED_PROVENANCE_VIEW", "exposure": corrected,
            "validation": exposure_validation(corrected, review, checkpoint),
            "binding": bind_tactical_identity(review, corrected, checkpoint)}


def exposure_validation(exposure, review, checkpoint):
    if not isinstance(exposure, dict) or exposure.get("schema") != SCHEMA or exposure.get("status") != "AVAILABLE":
        return {"valid": False, "status": "RAW_EVIDENCE_UNAVAILABLE", "reason": "RAW_EVIDENCE_UNAVAILABLE"}
    invalid = {"valid": False, "status": "EXPOSURE_VALIDATION_FAILED", "reason": "INVALID_EXPOSURE"}
    if exposure.get("symbol") != review.get("Symbol") or exposure.get("checkpoint") != checkpoint:
        return invalid
    if exposure.get("production_selected_displacement") != review.get("Displacement", "UNAVAILABLE"):
        return invalid
    try:
        for key in ("raw_directional_displacement", "structure_events", "liquidity_events", "zones"):
            seen = {}
            identical_duplicate = False
            for e in exposure[key]:
                normalized = _canonical_payload(e)
                if e["event_id"] in seen:
                    if seen[e["event_id"]] != normalized:
                        return {"valid": False, "status": "EXPOSURE_VALIDATION_FAILED",
                                "reason": "EVENT_ID_COLLISION_DIFFERENT_PAYLOAD", "collection": key}
                    identical_duplicate = True
                if (type(e["timestamp"]) is not int or e["timestamp"] <= 0 or type(e["available_at"]) is not int or
                    e["symbol"] != review["Symbol"] or
                    e["timestamp"] + TIMEFRAME_SECONDS[e["timeframe"]] > e["available_at"] or e["available_at"] > checkpoint):
                    return invalid
                seen[e["event_id"]] = normalized
            if identical_duplicate:
                return {"valid": False, "status": "EXPOSURE_VALIDATION_FAILED",
                        "reason": "DUPLICATE_EVENT_ID", "collection": key}
        parents = {e["event_id"]: e for e in exposure["raw_directional_displacement"]}
        for zone in exposure["zones"]:
            if zone.get("ancestry_version") != "ob-displacement-ancestry.v1":
                continue
            parent = parents.get(zone.get("related_displacement_event_id"))
            if (not parent or zone["type"] != "OB" or parent["timeframe"] != zone["timeframe"] or
                parent["direction"] != zone["direction"] or parent["timestamp"] <= zone["timestamp"] or
                zone["related_displacement_timestamp"] != parent["timestamp"] or
                zone["related_displacement_direction"] != parent["direction"] or
                zone["related_displacement_id"] != f"{parent['timeframe']}:{parent['timestamp']}" or
                zone["related_displacement_strength"] != parent["evidence"]["strength"] or
                zone["related_displacement_body_ratio"] != parent["evidence"]["body_ratio"] or
                zone["evidence"]["displacement_strength"] != parent["evidence"]["body_ratio"]):
                return {"valid": False, "status": "EXPOSURE_VALIDATION_FAILED", "reason": "OB_ANCESTRY_MISMATCH"}
        return {"valid": True, "status": "VALID", "reason": None}
    except (KeyError, TypeError, ValueError):
        return invalid


def valid_exposure(exposure, review, checkpoint):
    return exposure_validation(exposure, review, checkpoint)["valid"]


def bind_tactical_identity(review, exposure, checkpoint):
    """Bind only exact existing references; timing validates, never supplies a link."""
    out = {"schema": "tactical_setup_identity.v1", "status": "TACTICAL_IDENTITY_INCOMPLETE",
           "tactical_setup_id": None, "anchors": {}, "origin_creation_allowed": False,
           "reason": "RAW_EVIDENCE_UNAVAILABLE"}
    validation = exposure_validation(exposure, review, checkpoint)
    if not validation["valid"]:
        out["reason"] = validation["status"]
        out["validation_reason"] = validation["reason"]
        return out
    shadow = classify_tactical(review, checkpoint)
    out["missing_evidence"] = copy.deepcopy(shadow["missing_evidence"])
    direction = {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(shadow["tactical_direction"])
    h1 = _breaks(review, "H1", checkpoint)
    latest = [e for e in h1 if e["timestamp"] == max(x["timestamp"] for x in h1)] if h1 else []
    anchor = [e for e in exposure["structure_events"] if e["timeframe"] == "H1" and
              any(e["timestamp"] == x["timestamp"] and e["direction"] == x["direction"] and
                  e["type"] == x["type"].removeprefix("Last_") for x in latest)]
    match = re.fullmatch(r"(BULLISH|BEARISH) @ (\d+); related_sweep_id=(\w+):(\d+); related_displacement_id=(BULLISH|BEARISH):(\d+)", str(review.get("Contextual_MSS", "")))
    if len(anchor) != 1 or not match or match[1] != direction or match[5] != direction:
        out["reason"] = "EXPLICIT_H1_OR_CONTEXTUAL_REFERENCE_MISSING_OR_AMBIGUOUS"
        return out
    ds = [e for e in exposure["raw_directional_displacement"] if e["direction"] == direction and e["timestamp"] == int(match[6])]
    ms = [e for e in exposure["structure_events"] if e["timeframe"] == "M15" and e["type"] == "MSS"
          and e["direction"] == direction and e["timestamp"] == int(match[2])]
    sw = [e for e in exposure["liquidity_events"] if e["type"] == "SWEPT" and e["timeframe"] == match[3]
          and e["timestamp"] == int(match[4]) and e["side"] == ("SELLSIDE" if direction == "BULLISH" else "BUYSIDE")]
    zs = [e for e in exposure["zones"] if e["setup_id"] and e["setup_id"] == review.get("Active_Setup_ID")
          and e["direction"] == direction and len(ds) == 1 and e["timeframe"] == ds[0]["timeframe"]
          and e["evidence"].get("related_displacement_id") in
          (f"{direction}:{match[6]}", f"{ds[0]['timeframe']}:{match[6]}" )]
    if any(len(es) != 1 for es in (ds, ms, sw, zs)) or not sw[0].get("pool_id"):
        out["reason"] = "EXACT_REFERENCES_MISSING_OR_COLLIDING"
        return out
    d, m, s, z = ds[0], ms[0], sw[0], zs[0]
    rec = [e for e in exposure["liquidity_events"] if e["type"] == "RECLAIMED" and e.get("pool_id") == s["pool_id"]
           and e["timeframe"] == s["timeframe"] and s["timestamp"] <= e["timestamp"] <= d["timestamp"]]
    if len(rec) != 1:
        out["reason"] = "RECLAIM_IDENTITY_MISSING_OR_AMBIGUOUS"
        return out
    projection = copy.deepcopy(review)
    projection["Displacement"] = rv._displacement_text(rv.DisplacementEvent(**d["evidence"]))
    projection["Liquidity_Events"] = [s["evidence"], rec[0]["evidence"]]
    projection["FVG"] = [z["evidence"]]
    projection["FVG"] = copy.deepcopy(projection["FVG"])
    # Translate the detector's TF:timestamp reference only after exact TF/event resolution.
    projection["FVG"][0]["related_displacement_id"] = f"{direction}:{match[6]}"
    # Retain latest M15 requirement: a matching older event cannot override it.
    qualified = classify_tactical(projection, checkpoint)
    if qualified["state"] != "TACTICAL_CONFIRMED":
        out["reason"] = "EXISTING_V474_REQUIREMENTS_INCOMPLETE"
        out["missing_evidence"] = qualified["missing_evidence"]
        return out
    anchors = {"H1": anchor[0]["event_id"], "displacement": d["event_id"], "sweep": s["event_id"],
               "reclaim": rec[0]["event_id"], "M15": m["event_id"], "setup": z["setup_id"],
               "retest": review["Eligible_Retest_Evidence_ID"]}
    out.update(status="BOUND", anchors=anchors, reason=None,
               tactical_setup_id=identity("TACTICAL", review["Symbol"], direction, anchors), missing_evidence=[])
    return out
