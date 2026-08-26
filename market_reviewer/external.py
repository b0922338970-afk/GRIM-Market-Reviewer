"""External fetch and immutable market-data generation."""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

from .model import (
    ACTIVE_SYMBOLS,
    DataUnavailable,
    MIN_CLOSED_CANDLES,
    TIMEFRAME_SECONDS,
    Candle,
    new_generation_id,
    to_market_data_frame,
    utc_now_iso,
    validate_generation,
)
from .persistence import load_review_state
from .providers import CRYPTO_PROVIDER_ORDER, MarketDataProvider, default_crypto_providers


FETCH_SAFETY_BUFFER_BARS = 10
DEFAULT_BOOTSTRAP_CANDLES = 200
MAX_DYNAMIC_REPLAY_DAYS = 14
PROVIDER_MAX_SINGLE_REQUEST = {
    "bitunix_perpetual": 200,
    "binance": 1000,
    "coinbase": 300,
    "kraken": 720,
}


class ReplayHistoryTooOld(RuntimeError):
    """Raised when persisted state is beyond the supported dynamic replay horizon."""


class ReplayStateUnavailableForFetch(RuntimeError):
    """Raised when production replay fetch cannot load a valid review state."""


FETCH_MODE_BOOTSTRAP = "bootstrap"
FETCH_MODE_PRODUCTION_REPLAY = "production-replay"
FETCH_MODES = {FETCH_MODE_BOOTSTRAP, FETCH_MODE_PRODUCTION_REPLAY}


@dataclass(frozen=True)
class FetchDepthPlan:
    timeframe: str
    previous_review_timestamp: int | None
    target_timestamp: int
    replay_required_bars: int
    context_required_bars: int
    safety_buffer_bars: int
    requested_bars: int
    coverage_start_timestamp: int | None



def closed_candle_timestamp(candle: Candle, timeframe: str, fetch_timestamp: int) -> int | None:
    duration = TIMEFRAME_SECONDS[timeframe]
    return candle.timestamp if candle.timestamp + duration <= fetch_timestamp else None


def _target_closed_timestamp(fetch_timestamp: int, timeframe: str) -> int:
    seconds = TIMEFRAME_SECONDS[timeframe]
    return fetch_timestamp - (fetch_timestamp % seconds) - seconds


def _first_required_closed_timestamp(previous_timestamp: int, latest_closed_timestamp: int, timeframe: str) -> int | None:
    if latest_closed_timestamp <= previous_timestamp:
        return None
    seconds = TIMEFRAME_SECONDS[timeframe]
    offset = (latest_closed_timestamp - previous_timestamp) % seconds
    return previous_timestamp + (seconds if offset == 0 else offset)


def build_fetch_depth_plan(
    timeframe: str,
    fetch_timestamp: int,
    previous_review_timestamp: int | None,
    context_required_bars: int = MIN_CLOSED_CANDLES,
    safety_buffer_bars: int = FETCH_SAFETY_BUFFER_BARS,
    max_dynamic_replay_days: int = MAX_DYNAMIC_REPLAY_DAYS,
) -> FetchDepthPlan:
    target_timestamp = _target_closed_timestamp(fetch_timestamp, timeframe)
    if previous_review_timestamp is None:
        return FetchDepthPlan(timeframe, None, target_timestamp, 0, context_required_bars, safety_buffer_bars, DEFAULT_BOOTSTRAP_CANDLES, None)
    if fetch_timestamp - previous_review_timestamp > max_dynamic_replay_days * 86_400:
        raise ReplayHistoryTooOld("REPLAY_HISTORY_TOO_OLD")
    seconds = TIMEFRAME_SECONDS[timeframe]
    replay_required = max(0, math.ceil((target_timestamp - previous_review_timestamp) / seconds))
    requested = max(DEFAULT_BOOTSTRAP_CANDLES, replay_required + context_required_bars + safety_buffer_bars)
    first_required = _first_required_closed_timestamp(previous_review_timestamp, target_timestamp, timeframe)
    coverage_start = None if first_required is None else first_required - context_required_bars * seconds
    return FetchDepthPlan(timeframe, previous_review_timestamp, target_timestamp, replay_required, context_required_bars, safety_buffer_bars, requested, coverage_start)


def fetch_depth_plan_for_state(fetch_timestamp: int, state_path: Path | None = None) -> dict[str, dict[str, FetchDepthPlan]]:
    previous, _ = load_review_state(state_path)
    return _plans_from_previous(fetch_timestamp, previous)


def fetch_depth_plan_for_mode(
    fetch_timestamp: int,
    mode: str = FETCH_MODE_BOOTSTRAP,
    state_path: Path | None = None,
) -> dict[str, dict[str, FetchDepthPlan]]:
    if mode not in FETCH_MODES:
        raise ValueError(f"unknown fetch mode: {mode}")
    if mode == FETCH_MODE_PRODUCTION_REPLAY:
        previous, _ = load_required_replay_state(state_path)
        return _plans_from_previous(fetch_timestamp, previous)
    previous, _ = load_review_state(state_path)
    return _plans_from_previous(fetch_timestamp, previous)


def load_required_replay_state(state_path: Path | None) -> tuple[dict[str, dict], str]:
    if state_path is None:
        raise ReplayStateUnavailableForFetch("REPLAY_STATE_UNAVAILABLE_FOR_FETCH: state path required")
    if not state_path.exists():
        raise ReplayStateUnavailableForFetch(f"REPLAY_STATE_UNAVAILABLE_FOR_FETCH: missing state file: {state_path}")
    try:
        previous, loaded_from = load_review_state(state_path)
    except json.JSONDecodeError as exc:
        raise ReplayStateUnavailableForFetch(f"REPLAY_STATE_UNAVAILABLE_FOR_FETCH: malformed JSON: {state_path}") from exc
    missing_symbols = []
    for symbol in ACTIVE_SYMBOLS:
        try:
            timestamp = int((previous.get(symbol) or {}).get("previous_review_timestamp") or 0)
        except (TypeError, ValueError):
            timestamp = 0
        if timestamp <= 0:
            missing_symbols.append(symbol)
    if missing_symbols:
        missing = ",".join(missing_symbols)
        raise ReplayStateUnavailableForFetch(f"REPLAY_STATE_UNAVAILABLE_FOR_FETCH: missing previous_review_timestamp for {missing}")
    return previous, loaded_from


def replay_state_diagnostics(state_path: Path | None) -> dict[str, object]:
    try:
        previous, loaded_from = load_required_replay_state(state_path)
    except ReplayStateUnavailableForFetch as exc:
        return {
            "available": False,
            "state_source": str(state_path) if state_path is not None else None,
            "loaded_from": None,
            "error": str(exc),
        }
    return {
        "available": True,
        "state_source": str(state_path),
        "loaded_from": loaded_from,
        "previous_review_timestamp": {
            symbol: int(previous[symbol]["previous_review_timestamp"])
            for symbol in ACTIVE_SYMBOLS
        },
    }


def _plans_from_previous(fetch_timestamp: int, previous: dict[str, dict]) -> dict[str, dict[str, FetchDepthPlan]]:
    plans = {}
    for symbol in ACTIVE_SYMBOLS:
        previous_timestamp = None
        if symbol in previous:
            previous_timestamp = int(previous[symbol].get("previous_review_timestamp") or 0) or None
        plans[symbol] = {
            timeframe: build_fetch_depth_plan(timeframe, fetch_timestamp, previous_timestamp)
            for timeframe in TIMEFRAME_SECONDS
        }
    return plans


def _fetch_ohlcv(provider: MarketDataProvider, symbol: str, timeframe: str, limit: int, end_timestamp: int | None) -> list[Candle]:
    try:
        return provider.fetch_ohlcv(symbol, timeframe, limit=limit, end_timestamp=end_timestamp)  # type: ignore[call-arg]
    except TypeError:
        return provider.fetch_ohlcv(symbol, timeframe)


def _merge_pages(pages: list[list[Candle]]) -> list[Candle]:
    merged = {}
    for page in pages:
        for candle in page:
            merged[candle.timestamp] = candle
    return [merged[timestamp] for timestamp in sorted(merged)]


def _has_timestamp_gaps(candles: list[Candle], timeframe: str) -> bool:
    if len(candles) < 2:
        return False
    seconds = TIMEFRAME_SECONDS[timeframe]
    return any(right.timestamp - left.timestamp != seconds for left, right in zip(candles, candles[1:]))


def fetch_with_depth_plan(provider: MarketDataProvider, symbol: str, timeframe: str, plan: FetchDepthPlan) -> tuple[list[Candle], dict]:
    max_single = PROVIDER_MAX_SINGLE_REQUEST.get(provider.name, DEFAULT_BOOTSTRAP_CANDLES)
    pages = []
    remaining = plan.requested_bars
    end_timestamp: int | None = plan.target_timestamp + TIMEFRAME_SECONDS[timeframe]
    earliest_seen: int | None = None
    while remaining > 0:
        limit = min(remaining, max_single)
        page = _fetch_ohlcv(provider, symbol, timeframe, limit, end_timestamp)
        if not page:
            return [], {"pagination_pages": len(pages), "pagination_error": "EMPTY_PAGE"}
        pages.append(page)
        merged = _merge_pages(pages)
        if _has_timestamp_gaps(merged, timeframe):
            return [], {"pagination_pages": len(pages), "pagination_error": "TIMESTAMP_GAP"}
        if earliest_seen is not None and merged and merged[0].timestamp >= earliest_seen:
            return [], {"pagination_pages": len(pages), "pagination_error": "NO_PAGINATION_PROGRESS"}
        earliest_seen = merged[0].timestamp if merged else earliest_seen
        if plan.coverage_start_timestamp is not None and merged and merged[0].timestamp <= plan.coverage_start_timestamp:
            break
        if len(page) < limit or len(merged) >= plan.requested_bars:
            break
        end_timestamp = page[0].timestamp
        remaining = plan.requested_bars - len(merged)
    candles = _merge_pages(pages)
    if _has_timestamp_gaps(candles, timeframe):
        return [], {"pagination_pages": len(pages), "pagination_error": "TIMESTAMP_GAP"}
    diagnostics = {
        "requested_candle_count": plan.requested_bars,
        "returned_candle_count": len(candles),
        "replay_required_bars": plan.replay_required_bars,
        "context_required_bars": plan.context_required_bars,
        "safety_buffer_bars": plan.safety_buffer_bars,
        "coverage_start_timestamp": plan.coverage_start_timestamp,
        "pagination_pages": len(pages),
    }
    if plan.coverage_start_timestamp is not None and (not candles or candles[0].timestamp > plan.coverage_start_timestamp):
        diagnostics["pagination_error"] = "INSUFFICIENT_COVERAGE"
        return [], diagnostics
    return candles, diagnostics


def build_symbol_generation(
    symbol: str,
    provider: MarketDataProvider,
    raw_frames: dict[str, list[Candle]],
    fetch_timestamp: int,
    source_environment: str = "github_actions",
    fetch_diagnostics: dict[str, dict] | None = None,
) -> dict[str, dict]:
    generation_id = new_generation_id()
    generated_at = utc_now_iso()
    staged: dict[str, dict] = {}
    for timeframe, candles in raw_frames.items():
        closed = [c for c in candles if closed_candle_timestamp(c, timeframe, fetch_timestamp) is not None]
        current_open = None
        if len(closed) < len(candles):
            current_open = candles[len(closed)].timestamp
        latest_closed = closed[-1].timestamp if closed else 0
        diagnostics = (fetch_diagnostics or {}).get(timeframe, {})
        staged[timeframe] = {
            "symbol": symbol,
            "timeframe": timeframe,
            "source": "external_fetch",
            "provider": provider.name,
            "market_type": "crypto_perpetual" if provider.name == "bitunix_perpetual" else "crypto_spot",
            "timezone": "UTC",
            "dataset_id": f"market-data.v1:{symbol}:{timeframe}",
            "generation_id": generation_id,
            "generated_at": generated_at,
            "source_environment": source_environment,
            "completeness_status": "DATA_READY",
            "fetch_timestamp": fetch_timestamp,
            "latest_candle_timestamp": candles[-1].timestamp,
            "latest_closed_candle_timestamp": latest_closed,
            "current_open_candle_timestamp": current_open,
            "OHLCV": [c.__dict__ for c in candles],
            "status": "DATA_READY",
            "warnings": [],
            "requested_candle_count": diagnostics.get("requested_candle_count"),
            "returned_candle_count": diagnostics.get("returned_candle_count", len(candles)),
            "replay_required_bars": diagnostics.get("replay_required_bars"),
            "coverage_start_timestamp": diagnostics.get("coverage_start_timestamp"),
            "pagination_pages": diagnostics.get("pagination_pages", 1),
        }
    return staged


def fetch_external_generation(
    providers: list[MarketDataProvider],
    fetch_timestamp: int,
    depth_plans: dict[str, dict[str, FetchDepthPlan]] | None = None,
) -> dict[str, dict[str, dict]]:
    snapshot: dict[str, dict[str, dict]] = {}
    ordered = sorted(
        providers,
        key=lambda provider: CRYPTO_PROVIDER_ORDER.index(provider.name)
        if provider.name in CRYPTO_PROVIDER_ORDER
        else len(CRYPTO_PROVIDER_ORDER),
    )
    for symbol in ACTIVE_SYMBOLS:
        for provider in ordered:
            raw_frames: dict[str, list[Candle]] = {}
            diagnostics: dict[str, dict] = {}
            for timeframe in TIMEFRAME_SECONDS:
                plan = (depth_plans or {}).get(symbol, {}).get(timeframe)
                if plan is None:
                    candles = _fetch_ohlcv(provider, symbol, timeframe, DEFAULT_BOOTSTRAP_CANDLES, None)
                    diagnostic = {"requested_candle_count": DEFAULT_BOOTSTRAP_CANDLES, "returned_candle_count": len(candles), "replay_required_bars": 0, "coverage_start_timestamp": None, "pagination_pages": 1}
                else:
                    candles, diagnostic = fetch_with_depth_plan(provider, symbol, timeframe, plan)
                if not candles:
                    raw_frames = {}
                    break
                raw_frames[timeframe] = candles
                diagnostics[timeframe] = diagnostic
            if set(raw_frames) != set(TIMEFRAME_SECONDS):
                continue
            staged = build_symbol_generation(symbol, provider, raw_frames, fetch_timestamp, fetch_diagnostics=diagnostics)
            try:
                validate_generation({tf: to_market_data_frame(raw) for tf, raw in staged.items()})
            except DataUnavailable:
                continue
            snapshot[symbol] = staged
            break
        if symbol not in snapshot:
            raise RuntimeError("DATA_UNAVAILABLE")
    return snapshot


def latest_closed_index(snapshot: dict[str, dict[str, dict]]) -> dict[str, dict[str, int]]:
    return {
        symbol: {
            timeframe: frame["latest_closed_candle_timestamp"]
            for timeframe, frame in frames.items()
        }
        for symbol, frames in snapshot.items()
    }


def has_new_closed_candle(
    previous: dict[str, dict[str, int]] | None,
    current: dict[str, dict[str, int]],
) -> bool:
    if previous is None:
        return True
    for symbol, frames in current.items():
        for timeframe, timestamp in frames.items():
            if timestamp != previous.get(symbol, {}).get(timeframe):
                return True
    return False


def publish_artifact(snapshot: dict[str, dict[str, dict]], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    for symbol, frames in snapshot.items():
        validate_generation({tf: to_market_data_frame(raw) for tf, raw in frames.items()})
    path = output_dir / "market-data-v1.json"
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True), encoding="utf-8")
    return path


def run_external_fetch(
    output_dir: Path,
    fetch_timestamp: int | None = None,
    state_path: Path | None = None,
    mode: str = FETCH_MODE_BOOTSTRAP,
) -> Path:
    timestamp = int(time.time()) if fetch_timestamp is None else fetch_timestamp
    effective_state_path = state_path if state_path is not None else Path("reviews/thesis-baseline.json")
    if mode == FETCH_MODE_PRODUCTION_REPLAY:
        depth_plans = fetch_depth_plan_for_mode(timestamp, mode, effective_state_path)
    else:
        bootstrap_state = effective_state_path if effective_state_path.exists() else None
        depth_plans = fetch_depth_plan_for_mode(timestamp, mode, bootstrap_state)
    snapshot = fetch_external_generation(default_crypto_providers(), timestamp, depth_plans)
    return publish_artifact(snapshot, output_dir)
