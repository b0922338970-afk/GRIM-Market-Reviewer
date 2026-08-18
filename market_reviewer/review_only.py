"""CLI-friendly review-only entry points."""

from __future__ import annotations

import json
from pathlib import Path

from .pipeline import review_snapshot


def run_review_only(snapshot_path: str, thesis_path: str | None = None) -> str:
    reviews = review_snapshot(
        Path(snapshot_path),
        Path(thesis_path) if thesis_path else None,
    )
    return json.dumps(reviews, indent=2, sort_keys=True)
