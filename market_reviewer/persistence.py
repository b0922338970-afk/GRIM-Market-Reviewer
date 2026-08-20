"""Versioned review-state persistence."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .model import ACTIVE_SYMBOLS


PERSISTENCE_VERSION = 2
STATE_SCHEMA = "review-state.v2"


def load_review_state(path: Path | None) -> tuple[dict[str, dict], str]:
    if not path or not path.exists():
        return {}, "NONE"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("persistence_version") == PERSISTENCE_VERSION and isinstance(data.get("symbols"), dict):
        states = {
            symbol: _normalize_v2_state(state)
            for symbol, state in data["symbols"].items()
            if isinstance(state, dict)
        }
        return states, STATE_SCHEMA
    migrated = {
        symbol: _normalize_v2_state({
            **state,
            "state_loaded_from": "LEGACY_MIGRATED",
        })
        for symbol, state in data.items()
        if symbol in ACTIVE_SYMBOLS and isinstance(state, dict)
    }
    return migrated, "LEGACY_MIGRATED"


def _liquidity_side(level_type: str) -> str:
    if "Equal Lows" in level_type:
        return "EQL"
    if "Equal Highs" in level_type:
        return "EQH"
    if "Sell-side" in level_type:
        return "SSL"
    if "Buy-side" in level_type:
        return "BSL"
    return level_type.upper().replace(" ", "_")


def _liquidity_id(level: dict | None) -> str:
    if not level:
        return ""
    existing = level.get("liquidity_id")
    if existing:
        return str(existing)
    try:
        formed_at = int(level.get("formed_at", 0))
    except (TypeError, ValueError):
        formed_at = 0
    return f"{level.get('timeframe', '')}-{_liquidity_side(str(level.get('type', '')))}-{formed_at}"


def _with_liquidity_id(level: dict | None) -> dict | None:
    if not isinstance(level, dict):
        return level
    enriched = dict(level)
    enriched["liquidity_id"] = _liquidity_id(enriched)
    return enriched


def _retirement_record(target: dict | None, retired_at: int | None, sequence_id: str | None, reason: str | None) -> dict | None:
    target = _with_liquidity_id(target)
    if not target or not target.get("liquidity_id"):
        return None
    return {
        "liquidity_id": target["liquidity_id"],
        "price": target.get("price"),
        "type": target.get("type"),
        "timeframe": target.get("timeframe"),
        "formed_at": target.get("formed_at"),
        "status": "RETIRED_FOR_SEQUENCE_GENESIS",
        "retired_at": retired_at,
        "retired_by_sequence_id": sequence_id,
        "retired_reason": reason or "EXPIRED_NO_TRIGGER",
    }


def _append_unique_retirement(records: list[dict], record: dict | None) -> list[dict]:
    if not record:
        return records
    existing_ids = {item.get("liquidity_id") for item in records if isinstance(item, dict)}
    if record["liquidity_id"] not in existing_ids:
        records.append(record)
    return records


def _retired_from_history(state: dict) -> list[dict]:
    records: list[dict] = []
    for item in state.get("target_transition_history", []):
        if not isinstance(item, dict) or item.get("reason") != "EXPIRED_NO_TRIGGER":
            continue
        target = item.get("previous_target")
        transition = item.get("sequence_transition") if isinstance(item.get("sequence_transition"), dict) else {}
        records = _append_unique_retirement(
            records,
            _retirement_record(
                target,
                _optional_int(item.get("timestamp")) or _optional_int(transition.get("timestamp")),
                state.get("sequence_id"),
                item.get("reason"),
            ),
        )
    return records


def _normalize_v2_state(state: dict) -> dict:
    normalized = {
        **state,
        "persistence_version": PERSISTENCE_VERSION,
        "state_schema": STATE_SCHEMA,
    }
    normalized["active_tactical_draw"] = _with_liquidity_id(normalized.get("active_tactical_draw"))
    normalized["candidate_tactical_draw"] = _with_liquidity_id(normalized.get("candidate_tactical_draw"))
    retired = [dict(item) for item in normalized.get("retired_liquidity_instances", []) if isinstance(item, dict)]
    for item in retired:
        if "liquidity_id" not in item:
            item["liquidity_id"] = _liquidity_id(item)
    for record in _retired_from_history(normalized):
        retired = _append_unique_retirement(retired, record)
    normalized["retired_liquidity_instances"] = retired
    return normalized


def atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def persist_review_state(path: Path, reviews: dict[str, dict], previous: dict[str, dict]) -> dict:
    state = {
        "persistence_version": PERSISTENCE_VERSION,
        "state_schema": STATE_SCHEMA,
        "symbols": {
            symbol: _state_for_symbol(symbol, review, previous.get(symbol, {}))
            for symbol, review in reviews.items()
        },
    }
    atomic_write_json(path, state)
    return state


def _state_for_symbol(symbol: str, review: dict, previous: dict) -> dict:
    active = _level_from_text(review.get("Active_Tactical_Draw", "NONE"))
    candidate = _level_from_text(review.get("Candidate_Tactical_Draw", "NONE"))
    active_selected_at = _optional_int(review.get("Active_Draw_Selected_At"))
    sequence_started_at = _optional_int(review.get("Sequence_Started_At")) or active_selected_at

    if active:
        active["selected_at"] = active_selected_at
        active["status"] = review.get("Active_Draw_Status", "NONE")
        active = _with_liquidity_id(active)
    if candidate:
        candidate["detected_at"] = candidate.get("formed_at")
        candidate["status"] = review.get("Candidate_Draw_Status", "NONE")
        candidate = _with_liquidity_id(candidate)

    transitions = review.get("Sequence_Transitions", [])
    last_transition = transitions[-1] if transitions else previous.get("last_sequence_transition") or {
        "previous_state": "NONE",
        "new_state": review.get("Sequence_State", "UNKNOWN"),
        "timestamp": sequence_started_at or 0,
        "evidence": "state persisted without transition list",
    }
    history = list(previous.get("target_transition_history", []))
    retired = [dict(item) for item in previous.get("retired_liquidity_instances", []) if isinstance(item, dict)]
    if review.get("Target_Change_Reason") == "EXPIRED_NO_TRIGGER":
        retired = _append_unique_retirement(
            retired,
            _retirement_record(
                previous.get("active_tactical_draw"),
                _optional_int(review.get("Review_Timestamp")) or _review_timestamp_from_transition(last_transition),
                previous.get("sequence_id") or review.get("Sequence_ID"),
                "EXPIRED_NO_TRIGGER",
            ),
        )
    if review.get("Target_Changed") == "YES":
        history.append(
            {
                "previous_target": previous.get("active_tactical_draw"),
                "new_target": active,
                "timestamp": active_selected_at or _optional_int(review.get("Review_Timestamp")),
                "reason": review.get("Target_Change_Reason"),
                "structural_priority_evidence": review.get("Primary_POI", "NONE"),
                "sequence_transition": review.get("Sequence_Transitions", [])[-1] if review.get("Sequence_Transitions") else None,
            }
        )

    return {
        "persistence_version": PERSISTENCE_VERSION,
        "state_schema": STATE_SCHEMA,
        "symbol": symbol,
        "previous_bias": review.get("Swing_Bias"),
        "previous_regime": review.get("Market_Regime"),
        "current_phase": review.get("Current_Phase"),
        "previous_phase": previous.get("current_phase") or previous.get("previous_phase"),
        "previous_primary_target": review.get("Macro_Draw_on_Liquidity"),
        "previous_invalidation": review.get("Structural_Invalidation", {}).get("H1"),
        "previous_state": review.get("State"),
        "previous_score": review.get("Confidence"),
        "previous_review_timestamp": _optional_int(review.get("Review_Timestamp")) or _review_timestamp_from_transition(last_transition),
        "active_tactical_draw": active,
        "candidate_tactical_draw": candidate,
        "sequence_id": review.get("Sequence_ID"),
        "sequence_state": review.get("Sequence_State"),
        "sequence_started_at": sequence_started_at,
        "last_sequence_transition": last_transition,
        "target_changed": review.get("Target_Changed"),
        "target_change_reason": review.get("Target_Change_Reason"),
        "target_transition_history": history,
        "retired_liquidity_instances": retired,
    }


def _level_from_text(text: str) -> dict[str, Any] | None:
    if not text or text == "NONE":
        return None
    match = re.search(r"^(.*?) ([0-9]+(?:\.[0-9]+)?) on (\w+), formed_at=([0-9]+)", text)
    if not match:
        return None
    level = {
        "type": match.group(1),
        "price": float(match.group(2)),
        "timeframe": match.group(3),
        "formed_at": int(match.group(4)),
    }
    level["liquidity_id"] = _liquidity_id(level)
    return level


def _optional_int(value: Any) -> int | None:
    if value in {None, "NONE"}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _review_timestamp_from_transition(transition: dict) -> int:
    return _optional_int(transition.get("timestamp")) or 0
