"""Review-only pipeline."""

from __future__ import annotations

import json
from pathlib import Path

from .model import DataUnavailable, TIMEFRAMES, to_market_data_frame, validate_generation
from .reviewer import review_symbol


def load_snapshot(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DataUnavailable("snapshot root must be an object")
    return data


def review_snapshot(path: Path, thesis_path: Path | None = None) -> dict[str, dict]:
    snapshot = load_snapshot(path)
    previous = {}
    if thesis_path and thesis_path.exists():
        previous = json.loads(thesis_path.read_text(encoding="utf-8"))
    reviews = {}
    for symbol, raw_frames in snapshot.items():
        frames = {tf: to_market_data_frame(raw_frames[tf]) for tf in TIMEFRAMES}
        validate_generation(frames)
        review = review_symbol(frames, previous.get(symbol))
        reviews[symbol] = review.to_dict()
    return reviews
