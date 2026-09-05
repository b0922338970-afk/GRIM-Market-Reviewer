"""Historical synthetic observations; no live persistence or network entry points."""

from __future__ import annotations

import copy
import json
import re
from collections import Counter
from pathlib import Path

from .historical_data import (
    DEFAULT_CACHE, SAMPLE_SOURCE, digest, external_at_checkpoint, frames_at_checkpoint,
    isolated_directory, iso, load_historical_input, safe_write, timestamp,
)
from .missed_opportunity import empty_store, update_horizon_outcomes
from .missed_opportunity_live import _apply_symbol_observation, opportunity_evidence_from_snapshot
from .model import ACTIVE_SYMBOLS, DataUnavailable
from .opportunity import extract_opportunity_snapshot
from .persistence import build_review_state
from .reviewer import review_symbol

SCHEMA = "historical-opportunity-replay.v1"
MANIFEST_SCHEMA = "historical-replay-manifest.v1"
DEFAULT_OUTPUT = Path("research/historical-replay")
MAX_M5_CHECKPOINTS = 2016
REGIMES = (
    "STRONG_UPTREND", "STRONG_DOWNTREND", "TREND_PULLBACK", "RANGE", "REVERSAL",
    "HIGH_VOLATILITY", "LOW_VOLATILITY", "BREAKOUT_CONTINUATION", "FAKE_BREAKOUT",
    "LIQUIDATION_EVENT", "EVENT_DRIVEN", "WEEKEND_LOW_LIQUIDITY", "EXHAUSTION", "TREND_END",
)


def observation_id(symbol: str, checkpoint: int, visible_identity: str) -> str:
    return f"H-{symbol}-{checkpoint}-{digest(visible_identity)[:12]}"


def decision_hash(document: dict) -> str:
    return digest({
        "observations": document["observations"],
        "episode_decisions": {e["tracker_id"]: {
            key: e.get(key) for key in (
                "symbol", "direction", "origin_snapshot_timestamp", "origin_checkpoint", "origin_price",
                "origin_observation", "context_signature", "episode_status", "status", "snapshots",
                "context_break_timestamp", "context_break_reason", "converted_to_production",
                "production_sequence_id", "conversion_timestamp",
            )
        } for e in document["episodes"]},
    })


def apply_episode_observation(store: dict, symbol: str, review: dict, frames: dict,
                              opportunity: dict, external: dict, checkpoint: int, identity: str) -> None:
    """Share live genesis/break/conversion rules, operating only on an in-memory store."""
    _apply_symbol_observation(
        store=store, symbol=symbol, review=review, frames=frames,
        opportunity_snapshot=opportunity, external_evidence=external,
        observation_number=checkpoint // 300, preexisting_store=copy.deepcopy(store),
    )
    for record in store["records"]:
        if not record["tracker_id"].startswith("HIST-"):
            record["tracker_id"] = "HIST-" + record["tracker_id"]
            record["created_at"] = iso(checkpoint)
            record["origin_checkpoint"] = checkpoint
            record["origin_historical_observation_id"] = identity
            record["origin_left_censored"] = not store.get("previous_checkpoint")
        record["sample_source"] = SAMPLE_SOURCE
        for snapshot in record["snapshots"]:
            snapshot["sample_source"] = SAMPLE_SOURCE
            if "historical_observation_id" not in snapshot:
                snapshot["historical_observation_id"] = identity
                snapshot["checkpoint"] = checkpoint
                snapshot["available_at"] = checkpoint
                record["updated_at"] = iso(checkpoint)
    store["previous_checkpoint"] = checkpoint


def evaluate_outcomes(frozen: dict, m5_frame) -> dict:
    if not frozen.get("decisions_frozen") or frozen.get("decision_sha256") != decision_hash(frozen):
        raise ValueError("outcomes require intact frozen decision snapshots")
    result = copy.deepcopy(frozen)
    for episode in result["episodes"]:
        update_horizon_outcomes(episode, m5_frame)
    if decision_hash(result) != frozen["decision_sha256"]:
        raise ValueError("outcome calculation mutated decisions")
    result["outcome_data_cutoff"] = m5_frame.latest_closed_candle_timestamp
    return result


def replay_frames(frames: dict, *, symbol: str, start: int, end: int, step_minutes: int = 60,
                  provenance: dict | None = None, external_history: list | None = None,
                  dry_run: bool = False, max_observations: int = 2) -> dict:
    if symbol not in ACTIVE_SYMBOLS:
        raise ValueError("Phase 1 supports BTC/ETH")
    if start <= 0 or end < start or start % 300 or end % 300:
        raise ValueError("start/end must be increasing M5 close checkpoints")
    if step_minutes < 5 or step_minutes % 5:
        raise ValueError("step-minutes must be a positive multiple of 5")
    if max_observations < 1 or max_observations > 24:
        raise ValueError("dry-run max-observations must be 1..24")
    effective_end = min(end, start + (max_observations - 1) * step_minutes * 60) if dry_run else end
    checkpoints = list(range(start, effective_end + 1, 300))
    if len(checkpoints) > MAX_M5_CHECKPOINTS:
        raise ValueError("bounded replay exceeded 2016 M5 checkpoints; split the manifest window")
    closed_opens = {c.timestamp for c in frames["M5"].closed_candles()}
    if any(t - 300 not in closed_opens for t in checkpoints):
        raise DataUnavailable("historical replay M5 coverage gap")
    previous = {}
    store = empty_store(generated_at=iso(start))
    observations = []
    for checkpoint in checkpoints:
        visible = frames_at_checkpoint(frames, checkpoint)
        if visible["M5"].latest_closed_candle_timestamp != checkpoint - 300:
            raise DataUnavailable("checkpoint does not match latest available closed M5")
        review = review_symbol(visible, previous or None, "HISTORICAL_ISOLATED").to_dict()
        previous = build_review_state({symbol: review}, {symbol: previous})["symbols"][symbol]
        if (checkpoint - start) % (step_minutes * 60) and checkpoint != effective_end:
            continue
        opportunity = extract_opportunity_snapshot(review, visible)
        opportunity["sample_source"] = SAMPLE_SOURCE
        external = external_at_checkpoint(symbol, visible, checkpoint, external_history)
        external["sample_source"] = SAMPLE_SOURCE
        identity = observation_id(symbol, checkpoint, digest({"generation": visible["D1"].generation_id, "review": review}))
        apply_episode_observation(store, symbol, review, visible, opportunity, external, checkpoint, identity)
        observations.append({
            "historical_observation_id": identity, "sample_source": SAMPLE_SOURCE,
            "symbol": symbol, "checkpoint": checkpoint, "available_at": checkpoint,
            "data_cutoff": {tf: f.latest_closed_candle_timestamp for tf, f in visible.items()},
            "source_provenance": {
                tf: {"provider": f.provider, "source": f.source, "market_type": f.market_type,
                     "visible_generation": f.generation_id}
                for tf, f in visible.items()
            },
            "review": review, "decision_feature_snapshot": opportunity,
            "external_evidence": external,
            "opportunity_evidence": opportunity_evidence_from_snapshot(opportunity, external),
        })
    document = {
        "schema": SCHEMA, "sample_source": SAMPLE_SOURCE, "symbol": symbol,
        "start": start, "end": effective_end, "requested_end": end, "step_minutes": step_minutes,
        "status": "DRY_RUN" if dry_run else "COMPLETE",
        "initial_state_policy": "COLD_START_WITH_CLOSED_CANDLE_CONTEXT",
        "internal_m5_checkpoint_count": len(checkpoints), "observations": observations,
        "episodes": store["records"], "source_provenance": provenance or {},
        "decisions_frozen": True,
    }
    document["decision_sha256"] = decision_hash(document)
    return document


def _window_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}", value):
        raise ValueError("invalid historical window_id")
    return value


def run_window(*, symbol: str, start: int | str, end: int | str, source: Path | None = None,
               step_minutes: int = 60, output_dir: Path = DEFAULT_OUTPUT, cache_dir: Path = DEFAULT_CACHE,
               window_id: str | None = None, dry_run: bool = False, max_observations: int = 2,
               external_history: list | None = None, analysis_regime_label: str | None = None,
               sampling_reason: str = "EXPLICIT_USER_WINDOW") -> dict:
    start, end = timestamp(start), timestamp(end)
    if analysis_regime_label is not None and analysis_regime_label not in REGIMES:
        raise ValueError("unsupported analysis regime label")
    root = isolated_directory(output_dir, "output")
    window_id = _window_id(window_id or f"{symbol}-{start}-{end}")
    target = root / "windows" / f"{window_id}.json"
    frames, provenance = load_historical_input(symbol, source, cache_dir, cache=not dry_run)
    specification = {
        "symbol": symbol, "start": start, "end": end, "step_minutes": step_minutes,
        "source_sha256": provenance["sha256"], "external_sha256": digest(external_history or []),
        "analysis_regime_label": analysis_regime_label, "sampling_reason": sampling_reason,
    }
    if target.exists() and not dry_run:
        stored = json.loads(target.read_text(encoding="utf-8"))
        if stored.get("schema") != SCHEMA or stored.get("specification") != specification:
            raise ValueError("window identity conflicts with completed replay; use a new window_id")
        if stored.get("status") != "COMPLETE" or decision_hash(stored) != stored.get("decision_sha256"):
            raise ValueError("stored historical window is incomplete/corrupted")
        return {**stored, "resume_status": "SKIPPED_COMPLETED"}
    frozen = replay_frames(
        frames, symbol=symbol, start=start, end=end, step_minutes=step_minutes,
        provenance=provenance, external_history=external_history,
        dry_run=dry_run, max_observations=max_observations,
    )
    result = evaluate_outcomes(frozen, frames["M5"])
    result.update({
        "window_id": window_id, "specification": specification,
        "analysis_context": {"type": "ANALYSIS_LABEL", "regime": analysis_regime_label, "sampling_reason": sampling_reason},
    })
    for episode in result["episodes"]:
        episode["analysis_regime_label"] = analysis_regime_label
        episode["analysis_label_type"] = "OUTCOME_CONTEXT"
        episode["provenance"] = {"window_id": window_id, "source": provenance}
        episode["feature_availability"] = episode["snapshots"][-1]["opportunity_evidence"]
        episode["conversion_equivalent"] = episode["converted_to_production"]
    if not dry_run:
        safe_write(root, target, result)
        refresh_summary(root)
    return result


def _same_trajectory(left: dict, right: dict) -> bool:
    if left["symbol"] != right["symbol"] or left["direction"] != right["direction"]:
        return False
    if left["tracker_id"] == right["tracker_id"]:
        return True
    if left["context_signature_hash"] != right["context_signature_hash"]:
        return False
    # Overlapping cold starts can observe the same trajectory with different
    # origin timestamps. Require shared, identical context rather than price proximity.
    left_times = {s["snapshot_timestamp"]: s for s in left["snapshots"]}
    return any(
        s["snapshot_timestamp"] in left_times
        and s["opportunity_evidence"] == left_times[s["snapshot_timestamp"]]["opportunity_evidence"]
        and s["price"] == left_times[s["snapshot_timestamp"]]["price"]
        for s in right["snapshots"]
    )


def summarize(documents: list[dict], live_store: dict | None = None) -> dict:
    observations = {}
    episodes = []
    unresolved = []
    raw_episode_count = 0
    for document in sorted(documents, key=lambda d: (d["start"], d["window_id"])):
        if document.get("sample_source") != SAMPLE_SOURCE:
            raise ValueError("live document cannot enter historical summary")
        for observation in document["observations"]:
            # Different cold-start histories can differ at the same candle checkpoint.
            # Keep these as distinct replay-context observations, not independent episodes.
            key = observation["historical_observation_id"]
            if key in observations and observations[key] != observation:
                raise ValueError("historical observation identity conflict")
            observations[key] = observation
        for original in document["episodes"]:
            raw_episode_count += 1
            alias = next((e for e in episodes if _same_trajectory(e, original)), None)
            provenance = {"window_id": document["window_id"], "tracker_id": original["tracker_id"],
                          "origin_checkpoint": original["origin_checkpoint"],
                          "analysis_regime_label": original.get("analysis_regime_label")}
            if alias is not None:
                alias["alias_provenance"].append(provenance)
                continue
            overlaps = [
                e["tracker_id"] for e in episodes
                if e["symbol"] == original["symbol"] and e["direction"] == original["direction"]
                and max(e["origin_snapshot_timestamp"], original["origin_snapshot_timestamp"])
                <= min(e["snapshots"][-1]["snapshot_timestamp"], original["snapshots"][-1]["snapshot_timestamp"])
            ]
            if overlaps:
                unresolved.append({**provenance, "possible_aliases": overlaps,
                                   "status": "UNRESOLVED_OVERLAP_NOT_COUNTED"})
                continue
            episode = copy.deepcopy(original)
            episode["alias_provenance"] = [provenance]
            episodes.append(episode)
    domains = Counter()
    for observation in observations.values():
        for domain, value in observation["opportunity_evidence"].items():
            if value not in {"DATA_UNAVAILABLE", "UNAVAILABLE", "NOT_APPLICABLE"}:
                domains[domain] += 1
    live_count = len((live_store or {}).get("records", [])) if live_store is not None else None
    return {
        "schema": SCHEMA, "sample_source": SAMPLE_SOURCE, "episodes": episodes,
        "total_replay_observations": len(observations),
        "unique_checkpoint_count": len({(o["symbol"], o["checkpoint"]) for o in observations.values()}),
        "total_historical_episodes": len(episodes),
        "raw_window_episode_count": raw_episode_count,
        "unresolved_overlap_count": len(unresolved),
        "uncounted_episode_provenance": unresolved,
        "completed_episodes": sum(e["episode_status"] in {"BROKEN", "CLOSED"} for e in episodes),
        "completed_outcome_episodes": sum(all(v["horizon_status"] == "COMPLETE" for v in e["outcomes"].values()) for e in episodes),
        "episodes_by_symbol": dict(Counter(e["symbol"] for e in episodes)),
        "episodes_by_regime": dict(Counter(e.get("analysis_regime_label") or "UNLABELED" for e in episodes)),
        "episodes_by_direction": dict(Counter(e["direction"] for e in episodes)),
        "outcome_coverage": {
            horizon: dict(Counter(e["outcomes"][horizon]["horizon_status"] for e in episodes))
            for horizon in ("1H", "4H", "12H", "24H")
        },
        "evidence_availability_rates": {domain: domains[domain] / len(observations) if observations else None
            for domain in ("STRUCTURE", "LIQUIDITY", "MOMENTUM", "POSITIONING", "CROWDING",
                           "LIQUIDATION_CONTEXT", "LOCATION", "REMAINING_OPPORTUNITY")},
        "maturity_by_source": {"LIVE": live_count, "HISTORICAL_REPLAY": len(episodes),
                              "TOTAL": live_count + len(episodes) if live_count is not None else None},
        "live_count_status": "READ_ONLY_PROVIDED_STORE" if live_store is not None else "NOT_LOADED",
        "live_role": "FORWARD_VALIDATION", "calibration": "NOT_IMPLEMENTED",
    }


def refresh_summary(output_dir: Path = DEFAULT_OUTPUT, live_store: dict | None = None) -> dict:
    root = isolated_directory(output_dir, "output")
    documents = [json.loads(path.read_text(encoding="utf-8")) for path in sorted((root / "windows").glob("*.json"))]
    for document in documents:
        if document.get("status") != "COMPLETE" or decision_hash(document) != document.get("decision_sha256"):
            raise ValueError("cannot summarize an unfrozen/corrupted window")
    result = summarize(documents, live_store)
    safe_write(root, root / "historical-opportunity-replay.json", result)
    return result


def run_batch(manifest_path: Path, *, output_dir: Path = DEFAULT_OUTPUT, cache_dir: Path = DEFAULT_CACHE,
              dry_run: bool = False, max_observations: int = 2) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != MANIFEST_SCHEMA or manifest.get("sample_source") != SAMPLE_SOURCE:
        raise ValueError("unsupported historical window manifest")
    windows = manifest.get("windows")
    if not isinstance(windows, list) or len({w["window_id"] for w in windows}) != len(windows):
        raise ValueError("manifest must contain unique windows")
    results = []
    for window in windows:
        if window["status"] == "NOT_SELECTED":
            results.append({"window_id": window["window_id"], "status": "NOT_SELECTED"})
            continue
        if window["status"] != "READY":
            raise ValueError("manifest window must be READY or NOT_SELECTED")
        source = Path(window["source"])
        if not source.is_absolute():
            source = manifest_path.parent / source
        result = run_window(
            symbol=window["symbol"], start=window["start"], end=window["end"], source=source,
            step_minutes=window.get("step_minutes", 60), output_dir=output_dir, cache_dir=cache_dir,
            window_id=window["window_id"], dry_run=dry_run, max_observations=max_observations,
            analysis_regime_label=window.get("analysis_regime_label"),
            sampling_reason=window["sampling_reason"],
        )
        results.append({"window_id": window["window_id"], "status": result.get("resume_status", result["status"]),
                        "observation_count": len(result["observations"]), "episode_count": len(result["episodes"])})
    return {"schema": MANIFEST_SCHEMA, "sample_source": SAMPLE_SOURCE, "windows": results}
