"""Review-only pipeline."""

from __future__ import annotations

import json
from pathlib import Path

from .model import DataUnavailable, TIMEFRAMES, to_market_data_frame, validate_generation
from .persistence import load_review_state, persist_review_state
from .reviewer import review_symbol


def load_snapshot(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DataUnavailable("snapshot root must be an object")
    return data


def review_snapshot(path: Path, thesis_path: Path | None = None) -> dict[str, dict]:
    snapshot = load_snapshot(path)
    previous, loaded_from = load_review_state(thesis_path)
    reviews = {}
    for symbol, raw_frames in snapshot.items():
        frames = {tf: to_market_data_frame(raw_frames[tf]) for tf in TIMEFRAMES}
        validate_generation(frames)
        review = review_symbol(frames, previous.get(symbol), loaded_from)
        reviews[symbol] = review.to_dict()
    if thesis_path:
        persist_review_state(thesis_path, reviews, previous)
    return reviews
