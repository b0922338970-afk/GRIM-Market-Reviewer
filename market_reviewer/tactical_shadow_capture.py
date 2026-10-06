"""Forward non-canonical capture and post-COMPLETE shadow persistence.

No market source is read during ledger replay. Frozen detector outputs and
zone-local retest facts are the sole inputs; never a second SMC detector.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

from . import reviewer as rv
from .model import Candle, TIMEFRAMES, TIMEFRAME_SECONDS
from .persistence import atomic_write_json
from .tactical_direction_shadow import classify_tactical
from .tactical_sequence_shadow import _inputs, _verified_prefix, _identity, advance, select_target

INPUT_SCHEMA = "tactical-shadow-selection-input.v1"
LEDGER_SCHEMA = "tactical-shadow-sequence-ledger.v1"
CAPTURE_VERSION = "V4.7.6.5"
KEY = "tactical_shadow_selection"
SYMBOLS = ("BTC", "ETH")
MIN_OBSERVATION = 295
LEDGER_NAME = "tactical-shadow-sequence.json"
HEALTH_NAME = "tactical-shadow-health.json"
REVIEW_FIELDS = ("Symbol", "Review_Timestamp", "Swing_Bias", "Displacement", "Last_MSS", "Last_BOS",
                 "FVG", "Order_Blocks", "Current_Phase", "Liquidity")


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _read(path):
    return json.loads(Path(path).read_bytes())


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _zone_key(gap):
    data = asdict(gap)
    return digest({k: data[k] for k in ("timeframe", "direction", "formed_at", "upper", "lower", "related_displacement_id")})


def _locked_target_capture(prefix, previous, symbol, checkpoint):
    target = (previous or {}).get("active_target")
    if not target:
        return None, []
    level = rv.LiquidityLevel(**{k: target[k] for k in rv.LiquidityLevel.__dataclass_fields__})
    events = []
    for event in rv.find_liquidity_events(prefix[level.timeframe], [level]):
        data = asdict(event)
        events.append({"event_id": _identity("TARGET_EVENT", symbol, level.liquidity_id, data),
            "symbol": symbol, "timeframe": level.timeframe, "timestamp": event.timestamp,
            "available_at": checkpoint, "type": event.event_type, "evidence": data,
            "pool_id": level.liquidity_id, "side": target["side"], "pool_identity_state": "EXACT",
            "producer": "market_reviewer.reviewer.find_liquidity_events",
            "source": "FROZEN_SHADOW_ACTIVE_TARGET"})
    return copy.deepcopy(target), events


def locked_target_inputs(inputs, snapshot, previous):
    """Refresh only the independently locked reference, even off display lists."""
    target = snapshot.get("locked_target")
    if target is None:
        return inputs
    if target != (previous or {}).get("active_target"):
        raise ValueError("LOCKED_TARGET_CAPTURE_MISMATCH")
    events = snapshot.get("locked_target_events", [])
    result = copy.deepcopy(inputs)
    result["liquidity_events"] = [e for e in result["liquidity_events"]
        if not any(e["timeframe"] == x["timeframe"] and e["evidence"] == x["evidence"] for x in events)] + events
    return result


def _retest_scope(prefix, gap):
    frame = prefix[gap.timeframe]
    seconds = TIMEFRAME_SECONDS[gap.timeframe]
    candles = [c for c in frame.closed_candles() if c.timestamp > gap.formed_at]
    required = list(range(gap.formed_at + seconds, frame.latest_closed_candle_timestamp + seconds, seconds))
    complete = [c.timestamp for c in candles] == required
    refreshed = rv._refresh_setup_status(prefix, gap)
    # Only intersections feed the existing retest helper. Full-prefix status
    # already captures invalidations even when a candle jumped across the zone.
    touches = [c for c in candles if c.low <= gap.upper and c.high >= gap.lower]
    return {"gap": asdict(gap), "refreshed": asdict(refreshed), "complete": complete,
            "coverage_start": gap.formed_at + seconds, "coverage_end": frame.latest_closed_candle_timestamp,
            "expected_count": len(required), "returned_count": len(candles),
            "source_candles_sha256": digest([asdict(c) for c in candles]),
            "touch_candles": [{k: v for k, v in asdict(c).items() if k != "volume"} for c in touches],
            "producer": "reviewer._refresh_setup_status / _eligible_setup_retest"}


def build_snapshot(frames, review, exposure, checkpoint, observation, previous=None):
    if observation < MIN_OBSERVATION:
        raise ValueError("PRE_ACTIVATION_OBSERVATION")
    prefix = _verified_prefix(frames, review, exposure, checkpoint)
    if prefix is None:
        raise ValueError("SOURCE_REQUIRED_AT_CAPTURE")
    scoped = _inputs(review, exposure, checkpoint)
    direction = classify_tactical(review, checkpoint)
    levels = [l for tf in TIMEFRAMES for l in rv.find_liquidity(prefix[tf], rv.analyze_structure(prefix[tf]))]
    target, reason = (select_target(review, prefix, direction["tactical_direction"], checkpoint, [])
                      if direction["tactical_direction"] in {"LONG", "SHORT"} else
                      (None, "EXPLICIT_TACTICAL_DIRECTION_UNAVAILABLE"))
    gaps = { _zone_key(rv.FairValueGap(**e["evidence"])): rv.FairValueGap(**e["evidence"])
             for e in scoped["zones"]}
    if previous and previous.get("active_setup"):
        gap = rv.FairValueGap(**previous["active_setup"])
        gaps[_zone_key(gap)] = gap
    locked_target, locked_events = _locked_target_capture(prefix, previous, review["Symbol"], checkpoint)
    snapshot = {"schema": INPUT_SCHEMA, "capture_version": CAPTURE_VERSION,
        "observation_number": observation, "checkpoint": checkpoint, "symbol": review["Symbol"],
        **{k: direction[k] for k in ("primary_direction", "tactical_direction", "relationship")},
        "activation_status": "SHADOW_ONLY", "canonical": False, "origin_creation_allowed": False,
        "review": {k: copy.deepcopy(review[k]) for k in REVIEW_FIELDS if k in review}, "exposure": scoped,
        "ranking": {"levels": [asdict(l) for l in levels], "price": rv._current_price(prefix),
                    "producer": "market_reviewer.reviewer._liquidity_draws"},
        "selected_target": target, "selection_reason": reason,
        "locked_target": locked_target, "locked_target_events": locked_events,
        "source_identity": {tf: {"provider": prefix[tf].provider,
            "generation_id": prefix[tf].generation_id, "dataset_id": prefix[tf].dataset_id,
            "latest_closed": prefix[tf].latest_closed_candle_timestamp} for tf in TIMEFRAMES},
        "retest_scopes": {k: _retest_scope(prefix, g) for k, g in sorted(gaps.items())}}
    validate_snapshot(snapshot)
    return snapshot


def validate_snapshot(s):
    if (s["schema"] != INPUT_SCHEMA or s["capture_version"] != CAPTURE_VERSION
            or s["observation_number"] < MIN_OBSERVATION or s["symbol"] not in SYMBOLS
            or s["review"]["Symbol"] != s["symbol"] or s.get("canonical") is not False
            or s.get("origin_creation_allowed") is not False or s.get("activation_status") != "SHADOW_ONLY"):
        raise ValueError("INVALID_SELECTION_IDENTITY")
    cp = s["checkpoint"]
    if int(s["review"]["Review_Timestamp"]) + 300 != cp:
        raise ValueError("INVALID_SELECTION_CHECKPOINT")
    _inputs(s["review"], s["exposure"], cp)
    target_events = s.get("locked_target_events", [])
    locked = s.get("locked_target")
    if target_events:
        if locked is None:
            raise ValueError("LOCKED_TARGET_CAPTURE_MISSING")
        scoped = copy.deepcopy(s["exposure"])
        scoped["liquidity_events"] = target_events
        _inputs(s["review"], scoped, cp)
        for e in target_events:
            if (e.get("pool_id") != locked["liquidity_id"] or e["timeframe"] != locked["timeframe"]
                    or e["evidence"]["level_price"] != locked["price"]
                    or e["evidence"]["level_type"] != locked["type"] or e.get("side") != locked["side"]):
                raise ValueError("LOCKED_TARGET_EVENT_MISMATCH")
    d = classify_tactical(s["review"], cp)
    if any(s[k] != d[k] for k in ("primary_direction", "tactical_direction", "relationship")):
        raise ValueError("DIRECTION_MISMATCH")
    levels = [rv.LiquidityLevel(**l) for l in s["ranking"]["levels"]]
    if any(l.timeframe not in TIMEFRAME_SECONDS or l.formed_at + TIMEFRAME_SECONDS[l.timeframe] > cp for l in levels):
        raise ValueError("FUTURE_LIQUIDITY")
    bias = {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(s["tactical_direction"])
    _, _, winner = rv._liquidity_draws(bias, s["review"].get("Current_Phase"), levels, s["ranking"]["price"])
    inventory_matches = s["review"].get("Liquidity") == [asdict(l) for l in levels[-24:]]
    if s["selected_target"] is not None:
        target = s["selected_target"]
        if (winner is None or not winner.liquidity_id or any(target[k] != v for k, v in asdict(winner).items())
                or not inventory_matches
                or asdict(winner) not in s["review"]["Liquidity"] or target["selected_at"] != cp
                or target["available_at"] != cp or winner.timeframe not in rv.TACTICAL_TIMEFRAMES
                or target["side"] != ("SELLSIDE" if bias == "BULLISH" else "BUYSIDE")):
            raise ValueError("SELECTION_RANKING_MISMATCH")
    elif (winner and winner.liquidity_id and winner.timeframe in rv.TACTICAL_TIMEFRAMES
          and inventory_matches and asdict(winner) in s["review"]["Liquidity"]):
        raise ValueError("MISSING_DETERMINISTIC_TARGET")
    for key, scope in s["retest_scopes"].items():
        gap = rv.FairValueGap(**scope["gap"])
        refreshed = rv.FairValueGap(**scope["refreshed"])
        seconds = TIMEFRAME_SECONDS[gap.timeframe]
        if (key != _zone_key(gap) or key != _zone_key(refreshed)
                or scope["coverage_start"] != gap.formed_at + seconds
                or gap.formed_at + seconds > cp
                or scope["coverage_end"] != (cp // seconds) * seconds - seconds
                or scope["expected_count"] != max(0, (scope["coverage_end"] - gap.formed_at) // seconds)
                or (scope["complete"] and scope["expected_count"] != scope["returned_count"])):
            raise ValueError("RETEST_SCOPE_MISMATCH")
        times = []
        for data in scope["touch_candles"]:
            candle = Candle.from_mapping(data)
            candle.validate(cp)
            times.append(candle.timestamp)
            if not scope["coverage_start"] <= candle.timestamp <= scope["coverage_end"] or candle.timestamp + seconds > cp:
                raise ValueError("FUTURE_RETEST_CANDLE")
            if candle.timestamp % seconds or not (candle.low <= gap.upper and candle.high >= gap.lower):
                raise ValueError("INVALID_RETEST_INTERSECTION")
        if times != sorted(set(times)):
            raise ValueError("RETEST_CANDLE_ORDER")


def frozen_retest(snapshot, gap, selected_at):
    scope = snapshot["retest_scopes"].get(_zone_key(gap))
    if not scope or not scope["complete"]:
        return None, None
    refreshed = rv.FairValueGap(**scope["refreshed"])
    candles = [Candle.from_mapping(c) for c in scope["touch_candles"] if c["timestamp"] >= selected_at]
    frame = SimpleNamespace(closed_candles=lambda: candles)
    return refreshed, rv._eligible_setup_retest({gap.timeframe: frame}, refreshed)


def capture_inputs(frames, reviews, exposures, checkpoint, observation, output_dir):
    """No writes. Called while the just-reviewed frames still exist in memory."""
    bundle = {"capture_version": CAPTURE_VERSION, "observation_number": observation,
              "checkpoint": checkpoint, "symbols": {}, "status": "AVAILABLE"}
    try:
        path = Path(output_dir) / LEDGER_NAME
        previous = _load_ledger(path)["current"] if path.exists() else {}
        for symbol in SYMBOLS:
            bundle["symbols"][symbol] = build_snapshot(frames[symbol], reviews[symbol], exposures[symbol],
                                                       checkpoint, observation, previous.get(symbol))
    except Exception:
        bundle.update(status="UNAVAILABLE", reason="CAPTURE_FAILED", symbols={})
    return bundle


def _empty():
    return {"schema": LEDGER_SCHEMA, "activation_status": "SHADOW_ONLY", "origin_creation_allowed": False,
            "activation_observation": None, "latest_observation": None, "records": [], "current": {}}


def _fold(previous, snapshot):
    s = snapshot
    validate_snapshot(s)
    state = advance(previous, s["review"], s["exposure"], s["checkpoint"], s["observation_number"], frozen=s)
    if not state["sequence_id"] and state["tactical_direction"] in {"LONG", "SHORT"}:
        # A pending formation is not a sequence start until an exact anchor exists.
        state["sequence_state"] = "FORMING"
    return state


def _transitions(states, observation, checkpoint):
    return [{"symbol": symbol, "sequence_id": state["sequence_id"], "from_state": t["previous_state"],
             "to_state": t["new_state"], "observation": observation, "checkpoint": checkpoint,
             "evidence_refs": t["evidence"], "reason": t["evidence"].get("reason", "DETERMINISTIC_SHADOW_LIFECYCLE")}
            for symbol, state in states.items() for t in state["transitions"] if t["checkpoint"] == checkpoint]


def _load_ledger(path):
    ledger = _read(path)
    if (ledger.get("schema") != LEDGER_SCHEMA or ledger.get("origin_creation_allowed") is not False
            or ledger.get("activation_status") != "SHADOW_ONLY"):
        raise ValueError("CORRUPT_LEDGER")
    current, last = {}, None
    for record in ledger["records"]:
        obs = record["observation_number"]
        if obs < MIN_OBSERVATION or (last is not None and obs <= last):
            raise ValueError("CORRUPT_LEDGER_ORDER")
        bundle = _read(Path(path).parent / "tactical-shadow-inputs" / f"{obs}.json")
        if digest(bundle) != record["input_sha256"]:
            raise ValueError("FROZEN_INPUT_HASH_MISMATCH")
        if (bundle["status"] != "AVAILABLE" or bundle["observation_number"] != obs
                or bundle["checkpoint"] != record["checkpoint"] or set(bundle["symbols"]) != set(SYMBOLS)
                or record.get("production_isolation_verified") is not True):
            raise ValueError("CORRUPT_CAPTURE_RECEIPT")
        for symbol in SYMBOLS:
            s = bundle["symbols"][symbol]
            if s["symbol"] != symbol or s["observation_number"] != obs or s["checkpoint"] != record["checkpoint"]:
                raise ValueError("CORRUPT_SYMBOL_IDENTITY")
            state = _fold(current.get(symbol), s)
            if state != record["states"][symbol]:
                raise ValueError("STATE_REPLAY_MISMATCH")
            current[symbol] = state
        if record["transitions"] != _transitions(current, obs, record["checkpoint"]):
            raise ValueError("CORRUPT_TRANSITION_LOG")
        last = obs
    if (current != ledger["current"] or last != ledger["latest_observation"]
            or ledger["activation_observation"] != (ledger["records"][0]["observation_number"] if last else None)):
        raise ValueError("CORRUPT_LEDGER_HEAD")
    return ledger


def _health(root, status, observation):
    """Failure telemetry is separate and best-effort, never canonical."""
    try:
        path = root / HEALTH_NAME
        health = _read(path) if path.exists() else {"schema": "tactical-shadow-health.v1", "events": []}
        health["events"].append({"observation": observation, "status": status})
        atomic_write_json(path, health)
    except Exception:
        pass


def commit_after_complete(config, observation):
    """Single attempt, exclusive writer, inputs first then atomic ledger commit."""
    root = Path(config.output_dir)
    status, locked = "PERSISTENCE_FAILED", False
    lock = root / "tactical-shadow.lock"
    try:
        if config.dry_run:
            return {"status": "SKIPPED_DRY_RUN"}
        journal = _read(config.commit_journal_path)
        matches = [t for t in journal["transactions"] if t.get("observation_number") == observation]
        if (len(matches) != 1 or matches[0].get("status") != "COMPLETE"
                or matches[0].get("research_status") != "COMPLETE"):
            return {"status": "SKIPPED_NOT_COMPLETE"}
        tx = matches[0]
        bundle = tx.get("recovery_payload", {}).get("non_canonical_research_evidence", {}).get(KEY)
        if observation < MIN_OBSERVATION or not bundle or bundle.get("capture_version") != CAPTURE_VERSION:
            return {"status": "SKIPPED_NO_FORWARD_CAPTURE"}
        protected = {p: _sha(p) for p in (config.state_path, config.research_tracker_path, config.commit_journal_path)}
        if config.production_head_path.exists():
            protected[config.production_head_path] = _sha(config.production_head_path)
        root.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        locked = True
        path = root / LEDGER_NAME
        ledger = _load_ledger(path) if path.exists() else _empty()
        fingerprint = digest(bundle)
        existing = next((r for r in ledger["records"] if r["observation_number"] == observation), None)
        if existing:
            if existing["input_sha256"] != fingerprint:
                status = "CONFLICT"
                raise ValueError("CONFLICT")
            return {"status": "NOOP"}
        if (bundle.get("status") != "AVAILABLE" or bundle["observation_number"] != observation
                or bundle["checkpoint"] != tx["canonical_checkpoint"] or set(bundle["symbols"]) != set(SYMBOLS)):
            raise ValueError("INVALID_CAPTURE")
        if ledger["latest_observation"] and observation <= ledger["latest_observation"]:
            status = "CONFLICT"
            raise ValueError("BACKWARD_APPEND")
        processed = {r["observation_number"] for r in ledger["records"]}
        if any(MIN_OBSERVATION <= t.get("observation_number", 0) < observation
               and t.get("status") == "COMPLETE" and t["observation_number"] not in processed
               and t.get("recovery_payload", {}).get("non_canonical_research_evidence", {}).get(KEY, {}).get("capture_version") == CAPTURE_VERSION
               for t in journal["transactions"]):
            raise ValueError("EARLIER_FORWARD_CAPTURE_UNCOMMITTED")
        states = {}
        for symbol in SYMBOLS:
            s = bundle["symbols"][symbol]
            if s["symbol"] != symbol or s["observation_number"] != observation or s["checkpoint"] != bundle["checkpoint"]:
                raise ValueError("SYMBOL_IDENTITY_MISMATCH")
            states[symbol] = _fold(ledger["current"].get(symbol), s)
        transitions = _transitions(states, observation, bundle["checkpoint"])
        if any(_sha(p) != sha for p, sha in protected.items()):
            raise ValueError("CANONICAL_CHANGED_DURING_SHADOW_COMMIT")
        input_path = root / "tactical-shadow-inputs" / f"{observation}.json"
        if input_path.exists() and digest(_read(input_path)) != fingerprint:
            status = "CONFLICT"
            raise ValueError("INPUT_CONFLICT")
        if not input_path.exists():
            atomic_write_json(input_path, bundle)
        if any(_sha(p) != sha for p, sha in protected.items()):
            raise ValueError("CANONICAL_CHANGED_DURING_SHADOW_COMMIT")
        record = {"observation_number": observation, "checkpoint": bundle["checkpoint"],
                  "input_sha256": fingerprint, "states": states, "transitions": transitions,
                  "production_hash": tx.get("production_hash"), "production_isolation_verified": True}
        ledger["records"].append(record)
        ledger.update(current=states, latest_observation=observation,
                      activation_observation=ledger["activation_observation"] or observation)
        atomic_write_json(path, ledger)
        return {"status": "PERSISTED", "observation": observation}
    except Exception:
        _health(root, status, observation)
        return {"status": status, "observation": observation}
    finally:
        if locked:
            try:
                lock.unlink()
            except OSError:
                pass


def recover_completed(config):
    """Only version-marked frozen captures; never reconstruct old market history."""
    try:
        if config.dry_run:
            return
        journal = _read(config.commit_journal_path)
        path = Path(config.output_dir) / LEDGER_NAME
        ledger = _load_ledger(path) if path.exists() else _empty()
        processed = {r["observation_number"]: r["input_sha256"] for r in ledger["records"]}
        for tx in sorted(journal.get("transactions", []), key=lambda t: t.get("observation_number", 0)):
            if (tx.get("status") == "COMPLETE" and tx.get("observation_number", 0) >= MIN_OBSERVATION
                    and tx.get("recovery_payload", {}).get("non_canonical_research_evidence", {}).get(KEY)):
                bundle = tx["recovery_payload"]["non_canonical_research_evidence"][KEY]
                if processed.get(tx["observation_number"]) == digest(bundle):
                    continue
                if commit_after_complete(config, tx["observation_number"])["status"] not in {"PERSISTED", "NOOP"}:
                    break
    except Exception:
        pass


def live_status(root=Path("artifact")):
    from .tactical_sequence_metrics import sequence_metrics
    root = Path(root)
    result = {"schema": "tactical-shadow-live-status.v1", "read_only": True,
              "origin_creation_allowed": False, "sequence_metrics": None,
              "invalidated_semantics": "Backward-compatible alias of invalidation_transitions",
              "SHADOW_SEQUENCE_CAPTURE_READY": False,
              "SHADOW_SEQUENCE_VALIDATED": False, "activation_observation": None, "latest_observation": None,
              "symbols": {s: {"current_sequence": None, "direction": "UNAVAILABLE", "relationship": "UNAVAILABLE",
                  "state": "UNAVAILABLE", "target": None, "started_at": None, "last_transition": None,
                  "observations_seen": 0} for s in SYMBOLS},
              **{name: 0 for name in ("sequence_started", "MSS_confirmed", "setup_formed", "retest_pending",
                 "retest_confirmed", "invalidated", "persistence_failures", "conflicts")}}
    try:
        path = root / LEDGER_NAME
        health = _read(root / HEALTH_NAME) if (root / HEALTH_NAME).exists() else {"events": []}
        conflicts = sum(e["status"] == "CONFLICT" for e in health["events"])
        failures = sum(e["status"] == "PERSISTENCE_FAILED" for e in health["events"])
        result.update(conflicts=conflicts, persistence_failures=failures)
        if not path.exists():
            return {**result, "status": "NOT_ACTIVATED", "reasons": ["NO_DURABLE_FORWARD_CAPTURE"]}
        ledger = _load_ledger(path)
        records = ledger["records"]
        consecutive = 0
        prior = None
        for r in records:
            good = r["production_isolation_verified"] and all(s["evaluation_status"] == "AVAILABLE" for s in r["states"].values())
            consecutive = (consecutive + 1 if prior is not None and r["observation_number"] == prior + 1 else 1) if good else 0
            prior = r["observation_number"]
        incomplete_latest = any(e["observation"] > (ledger["latest_observation"] or 0) for e in health["events"])
        ready = consecutive >= 4 and not conflicts and not incomplete_latest and not (root / "tactical-shadow.lock").exists()
        transitions = [t for r in records for t in r["transitions"]]
        reconciled = sequence_metrics(records)
        metrics = {name: sum(t["to_state"] == state for t in transitions) for name, state in
                   (("sequence_started", "FORMING"), ("MSS_confirmed", "MSS_CONFIRMED"),
                    ("setup_formed", "SETUP_FORMED"), ("retest_pending", "RETEST_PENDING"),
                    ("retest_confirmed", "RETEST_CONFIRMED"), ("invalidated", "INVALIDATED"))}
        symbols = {}
        for symbol, state in ledger["current"].items():
            own = [t for t in transitions if t["symbol"] == symbol]
            symbols[symbol] = {"current_sequence": state["sequence_id"], "direction": state["tactical_direction"],
                "relationship": state["relationship"], "state": state["sequence_state"], "target": state["active_target"],
                "started_at": state.get("started_at"), "last_transition": own[-1] if own else None,
                "observations_seen": sum(symbol in r["states"] for r in records)}
        return {**result, "status": "PASS", "activation_observation": ledger["activation_observation"],
                "latest_observation": ledger["latest_observation"], "symbols": symbols, **metrics,
                "sequence_metrics": reconciled,
                **{k: reconciled[k] for k in ("unique_sequence_ids", "invalidated_unique_sequences",
                    "invalidation_transitions", "withdrawn_transitions", "reversal_transitions",
                    "still_active_sequences", "current_invalidated_sequences")},
                "persistence_failures": failures, "conflicts": conflicts, "consecutive_complete_observations": consecutive,
                "fresh_process_replay": "PASS", "SHADOW_SEQUENCE_CAPTURE_READY": ready,
                "SHADOW_SEQUENCE_VALIDATED": ready and metrics["MSS_confirmed"] > 0,
                "reasons": ([] if ready else ["CAPTURE_NOT_YET_ACCEPTED"]) +
                           ([] if ready and metrics["MSS_confirmed"] else ["NO_ACCEPTED_FORWARD_LINEAGE_PROGRESSION"])}
    except Exception:
        return {**result, "status": "UNAVAILABLE", "reasons": ["CORRUPT_OR_NONDETERMINISTIC_SHADOW_STORE"]}
