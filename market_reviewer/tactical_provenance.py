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


def identity(kind, *parts):
    data = json.dumps([SCHEMA, kind, *parts], sort_keys=True, separators=(",", ":"))
    return kind + "-" + hashlib.sha256(data.encode()).hexdigest()[:24]


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
        for kind, zones in (("FVG", rv.find_fvgs(frame, displacements)),
                            ("OB", rv.find_order_blocks(frame, structure, displacements))):
            for z in zones:
                data = asdict(z)
                item = expose(tf, kind, z.formed_at, data,
                              {k: data[k] for k in ("direction", "upper", "lower", "high", "low") if k in data})
                item.update(direction=z.direction, setup_id=rv._setup_id(z) if kind == "FVG" and z.setup_type == "SETUP_FVG" else None)
                result["zones"].append(item)
    matches = [d for d in result["raw_directional_displacement"] if d["selection_reason"] == "SELECTED_TEXT_MATCH"]
    for d in matches:
        d["selected_for_production"] = len(matches) == 1
        d["selection_reason"] = "EXACT_SELECTED_TEXT_MATCH" if len(matches) == 1 else "SELECTION_TIMEFRAME_AMBIGUOUS"
    for key in ("raw_directional_displacement", "structure_events", "liquidity_events", "zones"):
        result[key].sort(key=lambda e: (e["timestamp"], e["timeframe"], e["event_id"]))
    return result


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


def valid_exposure(exposure, review, checkpoint):
    if not isinstance(exposure, dict) or exposure.get("schema") != SCHEMA or exposure.get("status") != "AVAILABLE":
        return False
    if exposure.get("symbol") != review.get("Symbol") or exposure.get("checkpoint") != checkpoint:
        return False
    if exposure.get("production_selected_displacement") != review.get("Displacement", "UNAVAILABLE"):
        return False
    try:
        for key in ("raw_directional_displacement", "structure_events", "liquidity_events", "zones"):
            seen = set()
            for e in exposure[key]:
                if (type(e["timestamp"]) is not int or e["timestamp"] <= 0 or type(e["available_at"]) is not int or
                    e["event_id"] in seen or e["symbol"] != review["Symbol"] or
                    e["timestamp"] + TIMEFRAME_SECONDS[e["timeframe"]] > e["available_at"] or e["available_at"] > checkpoint):
                    return False
                seen.add(e["event_id"])
        return True
    except (KeyError, TypeError, ValueError):
        return False


def bind_tactical_identity(review, exposure, checkpoint):
    """Bind only exact existing references; timing validates, never supplies a link."""
    out = {"schema": "tactical_setup_identity.v1", "status": "TACTICAL_IDENTITY_INCOMPLETE",
           "tactical_setup_id": None, "anchors": {}, "origin_creation_allowed": False,
           "reason": "RAW_EVIDENCE_UNAVAILABLE"}
    if not valid_exposure(exposure, review, checkpoint):
        return out
    shadow = classify_tactical(review, checkpoint)
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
               tactical_setup_id=identity("TACTICAL", review["Symbol"], direction, anchors))
    return out
