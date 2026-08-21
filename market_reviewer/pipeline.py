"""Review-only pipeline."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from .model import DataUnavailable, MarketDataFrame, TIMEFRAMES, TIMEFRAME_SECONDS, to_market_data_frame, validate_generation
from .persistence import atomic_write_json, build_review_state, load_review_state, persist_review_state
from .reviewer import review_symbol


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


def review_snapshot(path: Path, thesis_path: Path | None = None) -> dict[str, dict]:
    snapshot = load_snapshot(path)
    previous, loaded_from = load_review_state(thesis_path)
    reviews = {}
    replayed_state = {}
    for symbol, raw_frames in snapshot.items():
        frames = {tf: to_market_data_frame(raw_frames[tf]) for tf in TIMEFRAMES}
        validate_generation(frames)
        review, symbol_state = _review_symbol_with_native_replay(frames, previous.get(symbol), loaded_from)
        reviews[symbol] = review
        replayed_state[symbol] = symbol_state
    if thesis_path:
        if replayed_state:
            atomic_write_json(thesis_path, {"persistence_version": 2, "state_schema": "review-state.v2", "symbols": replayed_state})
        else:
            persist_review_state(thesis_path, reviews, previous)
    return reviews