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
            symbol: {
                **state,
                "persistence_version": PERSISTENCE_VERSION,
                "state_schema": STATE_SCHEMA,
            }
            for symbol, state in data["symbols"].items()
            if isinstance(state, dict)
        }
        return states, STATE_SCHEMA
    migrated = {
        symbol: {
            **state,
            "persistence_version": PERSISTENCE_VERSION,
            "state_loaded_from": "LEGACY_MIGRATED",
        }
        for symbol, state in data.items()
        if symbol in ACTIVE_SYMBOLS and isinstance(state, dict)
    }
    return migrated, "LEGACY_MIGRATED"


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
    if candidate:
        candidate["detected_at"] = candidate.get("formed_at")
        candidate["status"] = review.get("Candidate_Draw_Status", "NONE")

    transitions = review.get("Sequence_Transitions", [])
    last_transition = transitions[-1] if transitions else {
        "previous_state": "NONE",
        "new_state": review.get("Sequence_State", "UNKNOWN"),
        "timestamp": sequence_started_at or 0,
        "evidence": "state persisted without transition list",
    }
    history = list(previous.get("target_transition_history", []))
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
    }


def _level_from_text(text: str) -> dict[str, Any] | None:
    if not text or text == "NONE":
        return None
    match = re.search(r"^(.*?) ([0-9]+(?:\.[0-9]+)?) on (\w+), formed_at=([0-9]+)", text)
    if not match:
        return None
    return {
        "type": match.group(1),
        "price": float(match.group(2)),
        "timeframe": match.group(3),
        "formed_at": int(match.group(4)),
    }


def _optional_int(value: Any) -> int | None:
    if value in {None, "NONE"}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _review_timestamp_from_transition(transition: dict) -> int:
    return _optional_int(transition.get("timestamp")) or 0
