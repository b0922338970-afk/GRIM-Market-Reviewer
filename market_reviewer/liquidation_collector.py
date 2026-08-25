"""Research-only Binance USD-M liquidation event collector.

The collector stores raw liquidation events append-only and derives deterministic
window aggregations for external-market-evidence.v1. It never writes production
review state.
"""

from __future__ import annotations

import argparse
import base64
import os
import hashlib
import json
import socket
import ssl
import struct
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

from .external_evidence_providers import instrument_mapping, phase1_metric, unavailable_phase1_metric
from .model import ACTIVE_SYMBOLS


BINANCE_USDM_FORCE_ORDER_STREAM = "wss://fstream.binance.com/stream?streams="
LIQUIDATION_STORE_VERSION = "liquidation-event-store.v1"
COVERAGE_COMPLETE = "COMPLETE"
COVERAGE_PARTIAL = "PARTIAL"
COVERAGE_UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class RawLiquidationEvent:
    symbol: str
    provider: str
    market_type: str
    event_id: str
    side: str
    price: float
    quantity: float
    notional_usd: float
    event_timestamp: int
    received_timestamp: int
    available_at: int
    raw: dict[str, Any]
    fingerprint: str


class LiquidationEventStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._fingerprints: dict[str, set[str]] = {}

    def append(self, event: RawLiquidationEvent) -> bool:
        fingerprints = self._fingerprints_for(event.symbol)
        if event.fingerprint in fingerprints:
            return False
        path = self.events_path(event.symbol)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(asdict(event), sort_keys=True) + "\n")
        fingerprints.add(event.fingerprint)
        return True

    def read_events(self, symbol: str) -> list[RawLiquidationEvent]:
        path = self.events_path(symbol)
        if not path.exists():
            return []
        events = []
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    events.append(RawLiquidationEvent(**json.loads(line)))
        return events

    def event_count(self, symbol: str) -> int:
        return len(self.read_events(symbol))

    def events_path(self, symbol: str) -> Path:
        return self.root / f"{symbol}.jsonl"

    def _fingerprints_for(self, symbol: str) -> set[str]:
        if symbol not in self._fingerprints:
            self._fingerprints[symbol] = {event.fingerprint for event in self.read_events(symbol)}
        return self._fingerprints[symbol]


class CoverageStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def load(self, symbol: str) -> dict[str, Any]:
        path = self.path(symbol)
        if not path.exists():
            return {
                "store_version": LIQUIDATION_STORE_VERSION,
                "symbol": symbol,
                "collector_started_at": None,
                "last_event_received_at": None,
                "last_stream_message_at": None,
                "disconnect_intervals": [],
                "reconnect_intervals": [],
                "coverage_status": COVERAGE_UNAVAILABLE,
            }
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, symbol: str, coverage: dict[str, Any]) -> None:
        self.path(symbol).write_text(json.dumps(coverage, indent=2, sort_keys=True), encoding="utf-8")

    def mark_started(self, symbol: str, timestamp: int) -> dict[str, Any]:
        coverage = self.load(symbol)
        coverage["collector_started_at"] = coverage.get("collector_started_at") or timestamp
        coverage["last_stream_message_at"] = timestamp
        coverage["coverage_status"] = COVERAGE_COMPLETE
        self.save(symbol, coverage)
        return coverage

    def mark_message(self, symbol: str, timestamp: int, event_received: bool = False) -> dict[str, Any]:
        coverage = self.load(symbol)
        coverage["last_stream_message_at"] = timestamp
        if event_received:
            coverage["last_event_received_at"] = timestamp
        if coverage.get("coverage_status") == COVERAGE_UNAVAILABLE:
            coverage["coverage_status"] = COVERAGE_COMPLETE
        self.save(symbol, coverage)
        return coverage

    def mark_disconnect(self, symbol: str, timestamp: int, reason: str = "DISCONNECT") -> dict[str, Any]:
        coverage = self.load(symbol)
        coverage.setdefault("disconnect_intervals", []).append({"start": timestamp, "end": None, "reason": reason})
        coverage["coverage_status"] = COVERAGE_PARTIAL
        self.save(symbol, coverage)
        return coverage

    def mark_reconnect(self, symbol: str, timestamp: int) -> dict[str, Any]:
        coverage = self.load(symbol)
        disconnects = coverage.setdefault("disconnect_intervals", [])
        if disconnects and disconnects[-1].get("end") is None:
            disconnects[-1]["end"] = timestamp
        coverage.setdefault("reconnect_intervals", []).append({"timestamp": timestamp})
        coverage["last_stream_message_at"] = timestamp
        coverage["coverage_status"] = COVERAGE_COMPLETE
        self.save(symbol, coverage)
        return coverage

    def path(self, symbol: str) -> Path:
        return self.root / f"{symbol}-coverage.json"


def parse_binance_force_order(payload: dict[str, Any], received_timestamp: int) -> RawLiquidationEvent:
    raw_order = payload.get("o", payload)
    provider_symbol = str(raw_order["s"])
    symbol = provider_symbol.removesuffix("USDT")
    mapping = instrument_mapping(symbol)
    side = binance_force_order_side_to_liquidation(str(raw_order["S"]))
    price = float(raw_order.get("ap") or raw_order.get("p"))
    quantity = float(raw_order.get("z") or raw_order.get("q"))
    event_timestamp = int(payload.get("E") or raw_order.get("T")) // 1000
    event_id = str(raw_order.get("l") or raw_order.get("t") or raw_order.get("T") or "")
    raw_identity = {
        "provider": mapping.provider,
        "symbol": symbol,
        "event_timestamp": event_timestamp,
        "price": price,
        "quantity": quantity,
        "side": side,
        "event_id": event_id,
        "order_status": raw_order.get("X"),
    }
    fingerprint = liquidation_fingerprint(raw_identity)
    return RawLiquidationEvent(
        symbol=symbol,
        provider=mapping.provider,
        market_type=mapping.market_type,
        event_id=event_id,
        side=side,
        price=price,
        quantity=quantity,
        notional_usd=price * quantity,
        event_timestamp=event_timestamp,
        received_timestamp=received_timestamp,
        available_at=received_timestamp,
        raw=payload,
        fingerprint=fingerprint,
    )


def binance_force_order_side_to_liquidation(order_side: str) -> str:
    # Binance force-order side is the forced close order side: SELL closes longs,
    # BUY closes shorts.
    if order_side == "SELL":
        return "LONG_LIQUIDATION"
    if order_side == "BUY":
        return "SHORT_LIQUIDATION"
    raise ValueError(f"unsupported force-order side: {order_side}")


def liquidation_fingerprint(identity: dict[str, Any]) -> str:
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def aggregate_liquidations(
    events: Iterable[RawLiquidationEvent],
    coverage: dict[str, Any],
    snapshot_timestamp: int,
    windows: tuple[int, ...] = (300, 900, 3600),
) -> dict[str, dict[str, Any]]:
    result = {}
    event_list = [event for event in events if event.available_at <= snapshot_timestamp]
    for seconds in windows:
        label = _window_label(seconds)
        status = _window_coverage_status(coverage, snapshot_timestamp, seconds)
        if status != COVERAGE_COMPLETE:
            result[label] = {
                "coverage_status": status,
                "long_liquidation_notional": None,
                "short_liquidation_notional": None,
                "total_liquidation_notional": None,
                "liquidation_imbalance": None,
                "event_count_long": None,
                "event_count_short": None,
            }
            continue
        lower = snapshot_timestamp - seconds
        window_events = [event for event in event_list if lower < event.event_timestamp <= snapshot_timestamp]
        long_notional = sum(event.notional_usd for event in window_events if event.side == "LONG_LIQUIDATION")
        short_notional = sum(event.notional_usd for event in window_events if event.side == "SHORT_LIQUIDATION")
        total = long_notional + short_notional
        result[label] = {
            "coverage_status": status,
            "long_liquidation_notional": long_notional,
            "short_liquidation_notional": short_notional,
            "total_liquidation_notional": total,
            "liquidation_imbalance": (short_notional - long_notional) / total if total else 0,
            "event_count_long": sum(1 for event in window_events if event.side == "LONG_LIQUIDATION"),
            "event_count_short": sum(1 for event in window_events if event.side == "SHORT_LIQUIDATION"),
        }
    return result


def liquidation_metrics_from_store(store: LiquidationEventStore, coverage_store: CoverageStore, symbol: str, snapshot_timestamp: int) -> dict[str, dict[str, Any]]:
    mapping = instrument_mapping(symbol)
    coverage = coverage_store.load(symbol)
    aggregates = aggregate_liquidations(store.read_events(symbol), coverage, snapshot_timestamp)
    metrics: dict[str, dict[str, Any]] = {}
    for label, aggregate in aggregates.items():
        for side in ("long", "short"):
            metric_id = f"{side}_liquidation_notional_{label}"
            value = aggregate[f"{side}_liquidation_notional"]
            status = aggregate["coverage_status"]
            if status == COVERAGE_COMPLETE:
                metrics[metric_id] = phase1_metric(metric_id, value, mapping, snapshot_timestamp, snapshot_timestamp, snapshot_timestamp, label, "USD_NOTIONAL", "USD_NOTIONAL", raw_value=value)
            else:
                metrics[metric_id] = unavailable_phase1_metric(metric_id, mapping, label)
                metrics[metric_id]["availability"] = "PARTIAL" if status == COVERAGE_PARTIAL else "UNAVAILABLE"
    if "long_liquidation_notional_5m" in metrics:
        metrics["long_liquidation_notional"] = dict(metrics["long_liquidation_notional_5m"])
        metrics["long_liquidation_notional"]["metric_id"] = "long_liquidation_notional"
    if "short_liquidation_notional_5m" in metrics:
        metrics["short_liquidation_notional"] = dict(metrics["short_liquidation_notional_5m"])
        metrics["short_liquidation_notional"]["metric_id"] = "short_liquidation_notional"
    return metrics


def liquidation_status(root: Path, symbols: tuple[str, ...] = ACTIVE_SYMBOLS) -> list[dict[str, Any]]:
    event_store = LiquidationEventStore(root)
    coverage_store = CoverageStore(root)
    statuses = []
    for symbol in symbols:
        coverage = coverage_store.load(symbol)
        disconnects = coverage.get("disconnect_intervals", [])
        statuses.append(
            {
                "symbol": symbol,
                "connected": coverage.get("coverage_status") == COVERAGE_COMPLETE,
                "coverage_start": coverage.get("collector_started_at"),
                "last_message": coverage.get("last_stream_message_at"),
                "last_event": coverage.get("last_event_received_at"),
                "disconnect_count": len(disconnects),
                "coverage_gaps": disconnects,
                "stored_event_count": event_store.event_count(symbol),
            }
        )
    return statuses


def collect_liquidations(root: Path, duration_seconds: int = 30, symbols: tuple[str, ...] = ACTIVE_SYMBOLS) -> dict[str, Any]:
    store = LiquidationEventStore(root)
    coverage = CoverageStore(root)
    now = int(time.time())
    for symbol in symbols:
        coverage.mark_started(symbol, now)
    stream_url = _stream_url(symbols)
    received = 0
    try:
        for payload in _read_websocket_json(stream_url, duration_seconds):
            received_at = int(time.time())
            stream = str(payload.get("stream", ""))
            data = payload.get("data", payload)
            raw_symbol = str(data.get("o", data).get("s", ""))
            symbol = raw_symbol.removesuffix("USDT")
            if symbol in symbols:
                coverage.mark_message(symbol, received_at, event_received=True)
                if store.append(parse_binance_force_order(data, received_at)):
                    received += 1
            for active_symbol in symbols:
                if stream.startswith(active_symbol.lower()):
                    coverage.mark_message(active_symbol, received_at)
    except Exception as exc:
        failed_at = int(time.time())
        for symbol in symbols:
            coverage.mark_disconnect(symbol, failed_at, exc.__class__.__name__)
        return {"connected": False, "received_event_count": received, "error": exc.__class__.__name__, "status": liquidation_status(root, symbols)}
    return {"connected": True, "received_event_count": received, "status": liquidation_status(root, symbols)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="liquidation-collector")
    parser.add_argument("command", choices=("collect", "status"))
    parser.add_argument("--root", default="artifact/liquidations")
    parser.add_argument("--duration", type=int, default=30)
    args = parser.parse_args(argv)
    root = Path(args.root)
    if args.command == "collect":
        print(json.dumps(collect_liquidations(root, args.duration), indent=2, sort_keys=True))
    else:
        print(json.dumps(liquidation_status(root), indent=2, sort_keys=True))
    return 0


def _window_coverage_status(coverage: dict[str, Any], snapshot_timestamp: int, seconds: int) -> str:
    started = coverage.get("collector_started_at")
    last_message = coverage.get("last_stream_message_at")
    if started is None or last_message is None:
        return COVERAGE_UNAVAILABLE
    if int(started) > snapshot_timestamp - seconds or int(last_message) < snapshot_timestamp:
        return COVERAGE_PARTIAL
    for gap in coverage.get("disconnect_intervals", []):
        gap_start = int(gap.get("start") or 0)
        gap_end = int(gap.get("end") or snapshot_timestamp)
        if gap_start < snapshot_timestamp and gap_end > snapshot_timestamp - seconds:
            return COVERAGE_PARTIAL
    return COVERAGE_COMPLETE


def _window_label(seconds: int) -> str:
    if seconds == 300:
        return "5m"
    if seconds == 900:
        return "15m"
    if seconds == 3600:
        return "1h"
    return f"{seconds}s"


def _stream_url(symbols: tuple[str, ...]) -> str:
    streams = "/".join(f"{instrument_mapping(symbol).instrument.lower()}@forceOrder" for symbol in symbols)
    return BINANCE_USDM_FORCE_ORDER_STREAM + streams


def _read_websocket_json(url: str, duration_seconds: int) -> Iterable[dict[str, Any]]:
    # Minimal websocket client for dry-run connectivity without adding deps.
    if not url.startswith("wss://"):
        raise ValueError("only wss websocket URLs are supported")
    host_and_path = url[6:]
    host, path = host_and_path.split("/", 1)
    path = "/" + path
    key = "R1JJTS1NYXJrZXQtUmV2aWV3ZXI="
    deadline = time.time() + duration_seconds
    with socket.create_connection((host, 443), timeout=10) as raw_socket:
        with ssl.create_default_context().wrap_socket(raw_socket, server_hostname=host) as sock:
            request = (
                f"GET {path} HTTP/1.1\r\n"
                f"Host: {host}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n\r\n"
            )
            sock.sendall(request.encode("ascii"))
            response = sock.recv(4096)
            if b" 101 " not in response.split(b"\r\n", 1)[0]:
                raise ConnectionError("websocket upgrade failed")
            sock.settimeout(1)
            while time.time() < deadline:
                try:
                    message = _read_ws_message(sock)
                except TimeoutError:
                    continue
                if message:
                    yield json.loads(message)


def _read_ws_message(sock: ssl.SSLSocket) -> str | None:
    header = _recv_exact(sock, 2)
    if not header:
        return None
    first, second = header[0], header[1]
    opcode = first & 0x0F
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    payload = _recv_exact(sock, length)
    if opcode == 8:
        raise ConnectionError("websocket closed")
    if opcode != 1:
        return None
    return payload.decode("utf-8")


def _recv_exact(sock: ssl.SSLSocket, length: int) -> bytes:
    chunks = []
    remaining = length
    while remaining:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("socket closed")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


if __name__ == "__main__":
    raise SystemExit(main())