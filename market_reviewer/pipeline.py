"""Review-only pipeline."""

from __future__ import annotations

import json
import math
from dataclasses import replace
from pathlib import Path

from .model import DataUnavailable, MarketDataFrame, TIMEFRAMES, TIMEFRAME_SECONDS, to_market_data_frame, validate_generation
from .persistence import atomic_write_json, build_review_state, load_review_state, persist_review_state
from .reviewer import review_symbol


REPLAY_SAFETY_BUFFER_BARS = 10


def load_snapshot(path: Path) -> dict[str, dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise DataUnavailable("snapshot root must be an object")
    return data


def _frame_at_checkpoint(frame: MarketDataFrame, checkpoint: int) -> MarketDataFrame:
    global_close_time = checkpoint + TIMEFRAME_SECONDS["M5"]
    closed = [candle for candle in frame.candles if candle.timestamp + TIMEFRAME_SECONDS[frame.timeframe] <= global_close_time]
    if not closed:
        raise DataUnavailable("checkpoint has no closed candles")
    latest_closed = closed[-1].timestamp
    current_open = next((candle for candle in frame.candles if candle.timestamp > latest_closed), None)
    candles = [candle for candle in frame.candles if candle.timestamp <= latest_closed]
    current_open_timestamp = None
    if current_open and current_open.timestamp + TIMEFRAME_SECONDS[frame.timeframe] > global_close_time:
        candles.append(current_open)
        current_open_timestamp = current_open.timestamp
    return replace(
        frame,
        latest_closed_candle_timestamp=latest_closed,
        latest_candle_timestamp=candles[-1].timestamp,
        current_open_candle_timestamp=current_open_timestamp,
        candles=candles,
    )


def _m5_checkpoints(frames: dict[str, MarketDataFrame], previous_timestamp: int) -> list[int]:
    return [
        candle.timestamp
        for candle in frames["M5"].closed_candles()
        if candle.timestamp > previous_timestamp
    ]


def _first_required_closed_timestamp(previous_timestamp: int, latest_closed_timestamp: int, timeframe: str) -> int | None:
    if latest_closed_timestamp <= previous_timestamp:
        return None
    seconds = TIMEFRAME_SECONDS[timeframe]
    offset = (latest_closed_timestamp - previous_timestamp) % seconds
    return previous_timestamp + (seconds if offset == 0 else offset)


def _frame_coverage_diagnostic(frame: MarketDataFrame, previous_timestamp: int) -> dict:
    closed = frame.closed_candles()
    earliest = closed[0].timestamp if closed else None
    latest = frame.latest_closed_candle_timestamp
    first_required = _first_required_closed_timestamp(previous_timestamp, latest, frame.timeframe)
    complete = first_required is None or earliest is not None and (earliest <= previous_timestamp or earliest <= first_required)
    missing_start = None
    missing_end = None
    missing_count = 0
    if not complete and earliest is not None and first_required is not None:
        seconds = TIMEFRAME_SECONDS[frame.timeframe]
        missing_start = first_required
        missing_end = min(earliest - seconds, latest)
        if missing_end >= missing_start:
            missing_count = ((missing_end - missing_start) // seconds) + 1
    required_bars = 0
    if latest > previous_timestamp:
        required_bars = math.ceil((latest - previous_timestamp) / TIMEFRAME_SECONDS[frame.timeframe]) + REPLAY_SAFETY_BUFFER_BARS
    return {
        "timeframe": frame.timeframe,
        "earliest_available_timestamp": earliest,
        "latest_closed_candle_timestamp": latest,
        "current_open_candle_timestamp": frame.current_open_candle_timestamp,
        "candle_count": len(closed),
        "first_required_timestamp": first_required,
        "missing_interval_start": missing_start,
        "missing_interval_end": missing_end,
        "estimated_missing_candle_count": missing_count,
        "required_bars_with_safety_buffer": required_bars,
        "coverage_complete": complete,
    }


def replay_coverage_report(snapshot_frames: dict[str, dict[str, MarketDataFrame]], previous: dict[str, dict]) -> dict:
    symbol_reports = {}
    complete = True
    previous_timestamps = [
        int(state.get("previous_review_timestamp") or 0)
        for state in previous.values()
        if state
    ]
    replay_start = min(previous_timestamps) if previous_timestamps else 0
    for symbol, frames in snapshot_frames.items():
        previous_timestamp = int((previous.get(symbol) or {}).get("previous_review_timestamp") or 0)
        timeframe_reports = {
            timeframe: _frame_coverage_diagnostic(frame, previous_timestamp)
            for timeframe, frame in frames.items()
        }
        symbol_complete = all(item["coverage_complete"] for item in timeframe_reports.values())
        complete = complete and symbol_complete
        symbol_reports[symbol] = {
            "previous_review_timestamp": previous_timestamp,
            "replay_coverage_complete": symbol_complete,
            "timeframes": timeframe_reports,
        }
    return {
        "Status": "REPLAY_COVERAGE_COMPLETE" if complete else "REPLAY_DATA_GAP",
        "REPLAY_COVERAGE_COMPLETE": "YES" if complete else "NO",
        "REPLAY_DATA_GAP": "NO" if complete else "YES",
        "PRODUCTION_NATIVE_SEQUENTIAL_REPLAY": "ALLOWED" if complete else "BLOCKED",
        "previous_review_timestamp": replay_start,
        "symbols": symbol_reports,
    }


def _review_symbol_with_native_replay(
    frames: dict[str, MarketDataFrame],
    previous_symbol_state: dict | None,
    state_loaded_from: str,
) -> tuple[dict, dict]:
    previous_timestamp = int((previous_symbol_state or {}).get("previous_review_timestamp") or 0)
    checkpoints = _m5_checkpoints(frames, previous_timestamp) if previous_symbol_state else []
    if not checkpoints:
        review = review_symbol(frames, previous_symbol_state, state_loaded_from).to_dict()
        state = build_review_state({review["Symbol"]: review}, {review["Symbol"]: previous_symbol_state or {}})["symbols"][review["Symbol"]]
        return review, state

    current_previous = previous_symbol_state or {}
    final_review: dict | None = None
    symbol = frames["D1"].symbol
    for checkpoint in checkpoints:
        checkpoint_frames = {timeframe: _frame_at_checkpoint(frame, checkpoint) for timeframe, frame in frames.items()}
        try:
            validate_generation(checkpoint_frames)
        except DataUnavailable:
            continue
        final_review = review_symbol(checkpoint_frames, current_previous, state_loaded_from).to_dict()
        current_previous = build_review_state({symbol: final_review}, {symbol: current_previous})["symbols"][symbol]
    if final_review is None:
        final_review = review_symbol(frames, previous_symbol_state, state_loaded_from).to_dict()
    return final_review, current_previous


def review_snapshot(path: Path, thesis_path: Path | None = None, enforce_replay_coverage: bool | None = None) -> dict[str, dict]:
    snapshot = load_snapshot(path)
    previous, loaded_from = load_review_state(thesis_path)
    snapshot_frames = {
        symbol: {tf: to_market_data_frame(raw_frames[tf]) for tf in TIMEFRAMES}
        for symbol, raw_frames in snapshot.items()
    }
    for frames in snapshot_frames.values():
        validate_generation(frames)
    if enforce_replay_coverage is None:
        canonical_path = str(thesis_path or "").replace('\\', "/")
        enforce_replay_coverage = canonical_path.endswith("/reviews/thesis-baseline.json")
    if previous and enforce_replay_coverage:
        coverage = replay_coverage_report(snapshot_frames, previous)
        if coverage["REPLAY_DATA_GAP"] == "YES":
            return {"REPLAY_DATA_GAP": coverage}
    reviews = {}
    replayed_state = {}
    for symbol, frames in snapshot_frames.items():
        review, symbol_state = _review_symbol_with_native_replay(frames, previous.get(symbol), loaded_from)
        reviews[symbol] = review
        replayed_state[symbol] = symbol_state
    if thesis_path:
        if replayed_state:
            atomic_write_json(thesis_path, {"persistence_version": 2, "state_schema": "review-state.v2", "symbols": replayed_state})
        else:
            persist_review_state(thesis_path, reviews, previous)
    return reviews
