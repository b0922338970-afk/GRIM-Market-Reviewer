"""Offline, content-addressed historical inputs and strict availability views."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .external_evidence import build_external_market_evidence
from .missed_opportunity_live import price_change_context_from_frames
from .model import DataUnavailable, TIMEFRAMES, TIMEFRAME_SECONDS, to_market_data_frame, validate_generation
from .persistence import atomic_write_json
from .pipeline import is_candle_available_at_checkpoint

SAMPLE_SOURCE = "HISTORICAL_REPLAY"
CACHE_SCHEMA = "historical-market-cache.v1"
DEFAULT_CACHE = Path("artifact/historical-market-data")


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()


def timestamp(value: str | int) -> int:
    if isinstance(value, int) or str(value).isdigit():
        return int(value)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("historical timestamps must specify UTC/offset")
    return int(parsed.timestamp())


def isolated_directory(path: Path, purpose: str) -> Path:
    """Permit dedicated runtime subtrees or a separate caller-owned directory."""
    resolved = path.resolve()
    for project in {Path.cwd().resolve(), Path(__file__).resolve().parents[1]}:
        if resolved == project or resolved in project.parents:
            raise ValueError("historical output cannot be a project root/ancestor")
        for live in (project / "reviews", project / "research", project / "artifact"):
            if resolved == live or live in resolved.parents:
                allowed = project / ("research/historical-replay" if purpose == "output" else "artifact/historical-market-data")
                if resolved != allowed and allowed not in resolved.parents:
                    raise ValueError("historical/live path isolation violation")
    return resolved


def safe_write(root: Path, path: Path, document: dict) -> None:
    if root.resolve() not in path.resolve().parents:
        raise ValueError("historical write escaped its root")
    if path.is_symlink():
        raise ValueError("historical output symlink forbidden")
    atomic_write_json(path, document)


def load_historical_input(symbol: str, source: Path | None, cache_dir: Path = DEFAULT_CACHE, *, cache: bool = True):
    root = isolated_directory(cache_dir, "cache")
    if source is None:
        pointer = json.loads((root / f"{symbol}-index.json").read_text(encoding="utf-8"))
        if pointer.get("schema") != CACHE_SCHEMA or pointer.get("sample_source") != SAMPLE_SOURCE:
            raise ValueError("unsupported historical cache index")
        key = pointer["sha256"]
        if len(key) != 64 or any(c not in "0123456789abcdef" for c in key):
            raise ValueError("invalid cache content identity")
        source = root / f"{key}.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    if raw.get("schema") == CACHE_SCHEMA:
        payload = raw["market_data"]
        if digest(payload) != raw["sha256"]:
            raise ValueError("historical cache checksum mismatch")
    else:
        payload = {symbol: raw[symbol]}
    frames = {tf: to_market_data_frame(payload[symbol][tf]) for tf in TIMEFRAMES}
    validate_generation(frames)
    if any(frame.symbol != symbol for frame in frames.values()):
        raise DataUnavailable("historical source symbol mismatch")
    for tf, frame in frames.items():
        for candle in frame.candles:
            if not all(math.isfinite(v) for v in (candle.open, candle.high, candle.low, candle.close, candle.volume)):
                raise DataUnavailable("non-finite historical candle")
        closed = frame.closed_candles()
        if any(c.timestamp + TIMEFRAME_SECONDS[tf] > frame.fetch_timestamp for c in closed):
            raise DataUnavailable("source declares a not-yet-closed candle closed")
        if any(b.timestamp - a.timestamp != TIMEFRAME_SECONDS[tf] for a, b in zip(closed, closed[1:])):
            raise DataUnavailable(f"historical candle gap: {symbol}/{tf}")
    key = digest(payload)
    metadata = {
        "schema": CACHE_SCHEMA, "sample_source": SAMPLE_SOURCE, "sha256": key,
        "symbol": symbol, "timeframes": {
            tf: {"provider": f.provider, "source": f.source, "market_type": f.market_type,
                 "earliest_closed": f.closed_candles()[0].timestamp,
                 "latest_closed": f.latest_closed_candle_timestamp, "count": len(f.closed_candles())}
            for tf, f in frames.items()
        },
    }
    if cache:
        target = root / f"{key}.json"
        if not target.exists():
            safe_write(root, target, {**metadata, "market_data": payload})
        elif digest(json.loads(target.read_text(encoding="utf-8"))["market_data"]) != key:
            raise ValueError("existing cache content corrupted")
        safe_write(root, root / f"{symbol}-index.json", metadata)
    return frames, metadata


def frames_at_checkpoint(frames: dict, checkpoint: int) -> dict:
    """No current-open candle object, future metadata, or future-derived ID escapes."""
    visible = {}
    for tf, frame in frames.items():
        candles = [c for c in frame.closed_candles() if is_candle_available_at_checkpoint(c, tf, checkpoint)]
        if not candles:
            raise DataUnavailable(f"no closed candles at checkpoint: {tf}")
        visible[tf] = replace(
            frame, candles=candles, fetch_timestamp=checkpoint, generated_at=iso(checkpoint),
            latest_candle_timestamp=candles[-1].timestamp,
            latest_closed_candle_timestamp=candles[-1].timestamp, current_open_candle_timestamp=None,
            warnings=[], source_environment=SAMPLE_SOURCE,
        )
    identity = digest({tf: [vars(c) for c in f.candles] for tf, f in visible.items()})
    for tf, frame in visible.items():
        frame.generation_id = f"HIST-{identity}"
        frame.dataset_id = f"HIST-{frame.symbol}-{tf}-{identity}"
    validate_generation(visible)
    return visible


def external_at_checkpoint(symbol: str, frames: dict, checkpoint: int, history: list | None = None) -> dict:
    selected = {}
    for item in history or []:
        if item.get("symbol") != symbol:
            continue
        source_time, available = item.get("source_timestamp"), item.get("available_at")
        if not isinstance(source_time, int) or not isinstance(available, int):
            continue
        if not 0 <= source_time <= available <= checkpoint:
            continue
        metric_id = item["metric_id"]
        previous = selected.get(metric_id)
        if previous and (available, source_time) == (previous["available_at"], previous["source_timestamp"]) and item != previous:
            raise ValueError("conflicting historical external evidence")
        if previous is None or (available, source_time) > (previous["available_at"], previous["source_timestamp"]):
            selected[metric_id] = dict(item)
    return build_external_market_evidence(symbol, checkpoint, selected, price_change_context_from_frames(frames, checkpoint))
