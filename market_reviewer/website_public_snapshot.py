"""Public-only website export. No decision, runtime or research writes.

Unknown textual values fail closed to UNAVAILABLE; arbitrary strings and nested
records are never public data. Publishers receive only this projection.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
import re
import time
from typing import Any, Mapping, Protocol

from .persistence import atomic_write_json
from .website_read_model import build_website_read_model

SCHEMA = "grim-website-public-snapshot.v1"
DEFAULT_OUTPUT = Path("artifact/website-public-snapshot.json")
DEFAULT_STALE_AFTER_SECONDS = 1800
SYMBOLS = ("BTC", "ETH")
UNAVAILABLE = "UNAVAILABLE"
CHANNELS = {"TELEGRAM", "DISCORD", "LINE", "GENERIC_WEBHOOK", "WEBHOOK"}
STATES = {"NO_TRADE", "WAIT", "WATCH", "ARMED"}
DIRECTIONS = {"LONG", "SHORT", "NONE", "BULLISH", "BEARISH", "NEUTRAL"}
TIMEFRAMES = {"D1", "H4", "H1", "M15", "M5"}
REGIMES = {"TREND_CONTINUATION", "TREND_PULLBACK", "REVERSAL_CANDIDATE",
           "RANGE", "TRANSITION", "STRONG_UP", "STRONG_DOWN", "HIGH_VOL", "LOW_VOL"}
REACTIONS = {"RECLAIMED", "REJECTED", "ACCEPTED_OUTSIDE", "UNRESOLVED",
             "SWEEP_RECLAIMED", "SWEEP_REJECTED", "SWEEP_ACCEPTED_OUTSIDE",
             "SWEEP_UNRESOLVED", "NONE"}
ZONES = {"NONE", "FRESH", "TESTED", "DEFENDED", "FAILED", "MULTIPLE_RELEVANT_ZONES",
         "CHAIN_LINKED_BPR", "AMBIGUOUS_BPR", "ISOLATED_BPR",
         "CONFIRMED_CHAIN_BREAKER", "BREAKER_CANDIDATE", "GENERIC_ROLE_REVERSAL",
         "NOT_CONFIRMED", "OTHER_CHAIN_LINKED_BPR", "OTHER_CONFIRMED_CHAIN_BREAKER"}
ZONES |= {prefix + state for prefix in ("CHAIN_LINKED_", "OTHER_CHAIN_", "ISOLATED_")
          for state in ("FRESH", "TESTED", "DEFENDED", "FAILED")}
# This is a presentation grammar for existing Reviewer _level_text/_draw_text,
# not a liquidity detector. Free-form reasons after the semicolon are excluded.
DRAW = re.compile(
    r"(?:(?:Macro|Tactical) Draw: )?"
    r"(?:(?:External|Internal) (?:Buy-side|Sell-side)(?: Liquidity)?|Equal (?:Highs|Lows)) "
    r"[0-9]+\.[0-9]+ on (?:D1|H4|H1|M15|M5), "
    r"(?:formed_at=[0-9]+, )?distance=[0-9]+\.[0-9]+"
)
STORY_FIELDS = (
    set(SYMBOLS), DIRECTIONS,
    {"CONTINUATION", "PULLBACK", "REVERSAL_CANDIDATE", "RANGE", "EXHAUSTION", "TRANSITION"},
    {"NONE", "SEEKING_LIQUIDITY", "SWEPT", "RECLAIMED", "DISPLACEMENT_CONFIRMED",
     "MSS_CONFIRMED", "SETUP_FVG_CREATED", "RETEST_PENDING", "INVALIDATED",
     "EXPIRED_NO_TRIGGER", "COMPLETED"}, STATES,
)


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _unwrap(value: Any) -> Any:
    if isinstance(value, dict) and "availability" in value:
        if value.get("availability") != "AVAILABLE" or value.get("reason") == "EVIDENCE_NOT_EXPOSED":
            return None
        return _unwrap(value.get("value"))
    return value


def _enum(value: Any, allowed: set[str]) -> str:
    value = _unwrap(value)
    return value if isinstance(value, str) and value in allowed else UNAVAILABLE


def _integer(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _bool(value: Any) -> bool | None:
    return value if type(value) is bool else None


def _draw(value: Any) -> Any:
    value = _unwrap(value)
    if isinstance(value, dict):
        return {k: _draw(value.get(k)) for k in ("htf", "tactical")}
    if value == "NONE":
        return "NONE"
    if isinstance(value, str):
        summary = value.split(";", 1)[0].strip()
        if len(summary) <= 200 and DRAW.fullmatch(summary):
            return summary
    return UNAVAILABLE


def _reaction(value: Any) -> str:
    value = _unwrap(value)
    parts = value.split("+") if isinstance(value, str) else []
    return "+".join(parts) if parts and all(p in REACTIONS for p in parts) else UNAVAILABLE


def _story(value: Any, symbol: str) -> str:
    if not isinstance(value, str):
        return UNAVAILABLE
    parts = [p.strip() for p in value.split(";", 1)[0].split("|")]
    if len(parts) != len(STORY_FIELDS) or parts[0] != symbol:
        return UNAVAILABLE
    return " | ".join(_enum(p, allowed) for p, allowed in zip(parts, STORY_FIELDS))


def build_public_snapshot(read_model: Mapping[str, Any], *, now: int | None = None,
                          stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS) -> dict:
    """Deterministic for the same model, export time and freshness policy.

    generated_at is export time, never substituted for Reviewer source time.
    Missing/future source timestamps or a lagging reviewer make freshness stale,
    without changing the review_state. Remote consumers must also age this file.
    """
    if type(stale_after_seconds) is not int or stale_after_seconds <= 0:
        raise ValueError("INVALID_STALE_AFTER_SECONDS")
    now = int(time.time()) if now is None else now
    if _integer(now) is None:
        raise ValueError("INVALID_GENERATED_AT")
    runtime = _dict(read_model.get("runtime"))
    source = _dict(read_model.get("source"))
    observation = _integer(source.get("review_observation"))
    production = _integer(runtime.get("production_latest_observation"))
    rows = _dict(read_model.get("symbols"))
    stamps = [_integer(_dict(rows.get(s)).get("generated_at")) for s in SYMBOLS]
    source_time = min(stamps) if all(s is not None and s > 0 for s in stamps) else None
    stale = (source_time is None or source_time > now or
             any(s is not None and s > now for s in stamps) or
             now - source_time > stale_after_seconds or observation is None or
             (production is not None and observation < production) or runtime.get("stale") is True)
    symbols = {}
    for symbol in SYMBOLS:
        row = _dict(rows.get(symbol))
        structure = _dict(row.get("structure"))
        symbols[symbol] = {
            "review_state": _enum(row.get("review_state"), STATES),
            "direction": _enum(row.get("direction"), DIRECTIONS),
            "timeframe": _enum(row.get("timeframe"), TIMEFRAMES),
            "htf_regime": _enum(row.get("htf_regime"), REGIMES),
            "market_story": _story(row.get("market_story"), symbol),
            "structure": {
                "active_draw": _draw(structure.get("active_draw")),
                "liquidity_reaction": _reaction(structure.get("liquidity_reaction")),
                "displacement": _enum(structure.get("displacement"), {"NONE", "WEAK", "VALID", "STRONG", "INVALID"}),
                **{k: _enum(structure.get(k), {"NONE", "GENERIC", "GENERIC_INVENTORY", "CONTEXTUAL"}) for k in ("MSS", "BOS")},
                **{k: _enum(structure.get(k), ZONES) for k in ("FVG", "OB", "Breaker")},
            },
        }
    evidence = _dict(read_model.get("evidence"))
    liquidations = _dict(evidence.get("liquidation"))
    notification = _dict(evidence.get("notification"))
    channels = notification.get("enabled_channels")
    channels = channels if isinstance(channels, list) else []
    if not channels:
        channels = [_dict(notification.get("last_delivery")).get("channel")]
    channels = sorted({s.upper() for s in channels if isinstance(s, str) and s.upper() in CHANNELS})
    summary = _dict(_dict(read_model.get("research")).get("summary"))
    ready = _dict(summary.get("next_review_ready"))
    return {
        "schema": SCHEMA, "generated_at": now, "source_updated_at": source_time,
        "review_observation": observation, "source_observation": observation,
        "stale_after_seconds": stale_after_seconds, "stale": bool(stale),
        "runtime": {"running": _bool(runtime.get("running")),
                    "production_latest_observation": production,
                    "research_latest_observation": _integer(runtime.get("research_latest_observation")),
                    "stale": bool(stale)},
        "symbols": symbols,
        "evidence": {
            "liquidation": {s: {"status": _enum(_dict(liquidations.get(s)).get("status"),
                                                {"CONNECTED", "DISCONNECTED"})} for s in SYMBOLS},
            "notification": {"channels": channels},
        },
        "research": {**{k: _integer(summary.get(k)) for k in (
            "historical_origins", "live_origins", "classifiable_live_origins", "outcome_complete_live_origins")},
                     "FIRST_REVIEW": _bool(ready.get("SAMPLE_READY")),
                     "CALIBRATION": _bool(ready.get("OUTCOME_READY"))},
        "execution": {"status": "RESERVED", "connected": False},
    }


class SnapshotPublisher(Protocol):
    """File/HTTP adapter contract: success returns None, failure raises.

    An HTTP implementation must get URL/auth only from environment, use bounded
    timeouts, no redirects/retries, and never return/log credentials or responses.
    No HTTP provider or network call is enabled by this foundation.
    """

    def publish(self, snapshot: dict) -> None: ...


class HttpSnapshotPublisher(SnapshotPublisher, Protocol):
    """Provider-neutral HTTP contract; deployment supplies the implementation.

    Implementations read endpoint/auth from environment only. They must POST/PUT
    JSON over HTTPS with a finite timeout, reject redirects and non-2xx results,
    and perform no retries. No HTTP adapter is configured or invoked here.
    """


def validate_public_snapshot(snapshot: dict) -> None:
    """Reject extra keys or non-public values before any publishing side effect."""
    symbols = _dict(snapshot.get("symbols"))
    research = _dict(snapshot.get("research"))
    model = {
        "source": {"review_observation": snapshot.get("review_observation")},
        "runtime": snapshot.get("runtime"),
        "symbols": {s: {**_dict(symbols.get(s)), "generated_at": snapshot.get("source_updated_at")}
                    for s in SYMBOLS},
        "evidence": {
            "liquidation": _dict(snapshot.get("evidence")).get("liquidation"),
            "notification": {"enabled_channels": _dict(
                _dict(snapshot.get("evidence")).get("notification")).get("channels")},
        },
        "research": {"summary": {**research, "next_review_ready": {
            "SAMPLE_READY": research.get("FIRST_REVIEW"),
            "OUTCOME_READY": research.get("CALIBRATION")}}},
    }
    rebuilt = build_public_snapshot(model, now=snapshot.get("generated_at"),
                                    stale_after_seconds=snapshot.get("stale_after_seconds"))
    if rebuilt != snapshot:
        raise ValueError("INVALID_PUBLIC_SNAPSHOT")


@dataclass(frozen=True)
class FilePublisher:
    path: Path = DEFAULT_OUTPUT

    def publish(self, snapshot: dict) -> None:
        validate_public_snapshot(snapshot)
        atomic_write_json(self.path, snapshot)


def publish_public_snapshot(snapshot: dict, publisher: SnapshotPublisher) -> dict:
    """Best effort boundary; failure details must not leak paths or credentials."""
    try:
        if snapshot.get("schema") != SCHEMA:
            return {"status": "PUBLISH_FAILED", "error": "INVALID_PUBLIC_SCHEMA"}
        validate_public_snapshot(snapshot)
        publisher.publish(deepcopy(snapshot))
        return {"status": "PUBLISHED", "error": None}
    except Exception:
        return {"status": "PUBLISH_FAILED", "error": "PUBLISHER_ERROR"}


def export_public_snapshot(*, output: Path = DEFAULT_OUTPUT, now: int | None = None,
                           stale_after_seconds: int = DEFAULT_STALE_AFTER_SECONDS,
                           builder=None, publisher: SnapshotPublisher | None = None) -> dict:
    """Manual exporter; any future runner hook belongs strictly after COMPLETE."""
    try:
        model = (builder or build_website_read_model)()
        snapshot = build_public_snapshot(model, now=now, stale_after_seconds=stale_after_seconds)
        return publish_public_snapshot(snapshot, publisher if publisher is not None else FilePublisher(output))
    except Exception:
        return {"status": "PUBLISH_FAILED", "error": "SNAPSHOT_BUILD_FAILED"}
