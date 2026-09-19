"""Origin-time adapter for the existing research engines; never a decision gate."""
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from datetime import datetime, timezone
import json

from .model import TIMEFRAME_SECONDS

VERSION = "live-hybrid-smc-payload.v1"


def legal_prefix(frames, symbol, checkpoint):
    visible = {}
    for tf, seconds in TIMEFRAME_SECONDS.items():
        f = frames[tf]
        if f.symbol != symbol or f.timeframe != tf:
            raise ValueError("FRAME_IDENTITY_MISMATCH")
        candles = [c for c in f.closed_candles() if c.timestamp + seconds <= checkpoint]
        if not candles:
            raise ValueError("NO_LEGAL_PREFIX:" + tf)
        visible[tf] = replace(f, candles=candles, fetch_timestamp=checkpoint,
            generated_at=datetime.fromtimestamp(checkpoint, timezone.utc).isoformat(),
            dataset_id=f"LIVE-PREFIX-{symbol}-{tf}-{checkpoint}",
            generation_id=f"LIVE-PREFIX-{symbol}-{checkpoint}", warnings=[],
            latest_candle_timestamp=candles[-1].timestamp,
            latest_closed_candle_timestamp=candles[-1].timestamp,
            current_open_candle_timestamp=None)
    return visible


def build_live_hybrid_smc(record, review, opportunity, frames):
    """Returns unavailable on missing inputs, without vetoing the existing origin."""
    report = {"schema": VERSION, "status": "UNAVAILABLE", "reason": None, "smc_state": None}
    try:
        stamp = record["origin_snapshot_timestamp"]
        cp = stamp + 300
        if opportunity.get("snapshot_timestamp") != stamp or frames["M5"].latest_closed_candle_timestamp != stamp:
            raise ValueError("ORIGIN_BOUNDARY_MISMATCH")
        prefix = legal_prefix(frames, record["symbol"], cp)
        from historical_research import raw_prefix_smc as raw
        from historical_research import parent_attribution as single
        from historical_research import liquidity_cluster as cluster
        from historical_research.hybrid_attribution import resolve
        from historical_research.hybrid_chain_state import extensions, freeze_origin
        from historical_research.smc_origin_context import extract as extract_context

        # Whitelist decision inputs. The record's outcomes/snapshots never enter an engine.
        fs = {k: deepcopy(opportunity[k]) for k in ("raw_metrics", "truth", "features", "risk_signatures")}
        o = {"checkpoint": cp, "available_at": cp, "symbol": record["symbol"],
             "data_cutoff": {tf: f.latest_closed_candle_timestamp for tf, f in prefix.items()},
             "review": review, "decision_feature_snapshot": fs}
        context = extract_context(o, record["direction"])
        context["episode_id"] = record["tracker_id"]
        source_hash = sha256(json.dumps({tf: [vars(c) for c in f.candles] for tf, f in prefix.items()}, sort_keys=True).encode()).hexdigest()
        source = {"type": "LIVE_ORIGIN_LEGAL_PREFIX", "sha256": source_hash, "checkpoint": cp}
        _, events, _ = raw.extract(prefix, record["symbol"], cp, source)
        if any(e["available_at"] > cp for e in events.values()):
            raise ValueError("POST_ORIGIN_EVENT")
        graph = raw.reconstruct({"episode_id": record["tracker_id"], "origin_checkpoint": cp,
                                 "events": events, "inventory_keys": set()}, prefix)
        parents = single.resolve_graph(graph, prefix)
        clusters = cluster.reconstruct(graph, parents, prefix)
        hybrid = resolve(graph, parents, clusters)
        ext = extensions(graph, hybrid, prefix)
        state = freeze_origin(graph, hybrid, ext, context)
        state["matching_context"] = {
            "symbol": state["symbol"], "direction": state["direction"], "phase": state["phase"],
            "regime": review["Market_Regime"], "HTF": [review["Structure_State"]["D1"], review["Structure_State"]["H4"]],
            "location": state["fields"]["location"], "extension": state["fields"]["extension"],
            "liquidity_type": state["existing_context"]["SWEEP_QUALITY"].split("/")[0],
            "displacement": state["existing_context"]["DISPLACEMENT_QUALITY"],
        }
        state["producer_provenance"] = {"version": VERSION, "source": source,
            "closed_cutoffs": o["data_cutoff"], "event_count": len(events),
            "inventory_visibility": "NOT_EVALUATED", "outcomes_used": False}
        values = list(state["matching_context"].values())
        values += state["matching_context"]["HTF"]
        if any(v is None or v == "" or (isinstance(v, str) and v in {"UNKNOWN", "UNAVAILABLE", "N/A"}) for v in values):
            raise ValueError("MATCHING_CONTEXT_UNAVAILABLE")
        report.update(status="AVAILABLE", smc_state=state)
    except Exception as exc:
        # Capture failures are research availability, never origin eligibility or production failures.
        report["reason"] = type(exc).__name__ + ":" + str(exc)
    return report
