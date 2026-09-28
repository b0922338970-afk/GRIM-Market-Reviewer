"""Best-effort packet delivery; no market interpretation or execution policy."""
import hashlib
import json
import os
import time
from copy import deepcopy
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from urllib.request import Request, HTTPRedirectHandler, build_opener

from .persistence import atomic_write_json

SCHEMA = "notification-delivery.v1"
CHANNELS = ("TELEGRAM", "DISCORD", "LINE", "WEBHOOK")
DEFAULT_JOURNAL = Path("artifact/notification-delivery.json")
LEVELS = {"NO_TRADE": "SILENT", "WAIT": "SILENT", "WATCH": "NORMAL_ALERT", "ARMED": "HIGH_PRIORITY_ALERT"}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def alert_identity(packet):
    # Includes timestamp, immutable setup/context identities and every packet field.
    return hashlib.sha256(canonical(packet).encode()).hexdigest()


def render_message(packet):
    context = packet.get("quality_context") or {}
    def display(value):
        if isinstance(value, dict) and "availability" in value:
            return display(value.get("value")) if value["availability"] == "AVAILABLE" else "UNAVAILABLE"
        return "UNAVAILABLE" if value is None else value if isinstance(value, str) else canonical(value)
    priority = "HIGH PRIORITY" if packet["alert_level"] == "HIGH_PRIORITY_ALERT" else packet["alert_level"]
    rows = [("Symbol / TF", f"{packet.get('symbol')} / {packet.get('timeframe')}"),
            ("State", packet.get("review_state")), ("Direction", packet.get("direction")), ("Priority", priority),
            ("Market Story", packet.get("market_story")), ("HTF Draw", context.get("active_draw")),
            ("Liquidity Reaction", context.get("liquidity_reaction")), ("Displacement", context.get("displacement_quality")),
            ("Contextual MSS", context.get("contextual_mss")), ("Contextual BOS", context.get("contextual_bos")),
            ("FVG", context.get("fvg_state")), ("BPR", context.get("bpr_state")),
            ("OB", context.get("ob_state")), ("Breaker", context.get("breaker_state")),
            ("Entry Zone", packet.get("entry_zone")), ("Invalidation", packet.get("invalidation")),
            ("Target", packet.get("target")), ("Remaining Opportunity", packet.get("remaining_room")),
            ("Fragility", packet.get("fragility")), ("Generated At", packet.get("generated_at"))]
    return "[GRIM]\n" + "\n".join(f"{label}: {display(value)}" for label, value in rows) + "\nNotification only. No order has been placed."


def adapter_request(channel, env, packet, identity):
    prefix = "GRIM_NOTIFY_" + channel + "_"
    if env.get(prefix + "ENABLED", "").lower() != "true":
        return None, "SKIPPED_DISABLED"
    required = {"TELEGRAM": ("TOKEN", "CHAT_ID"), "DISCORD": ("URL",),
                "LINE": ("TOKEN", "TO"), "WEBHOOK": ("URL",)}[channel]
    if not all(env.get(prefix + key) for key in required):
        return None, "SKIPPED_NOT_CONFIGURED"
    text = render_message(packet)
    headers = {"Content-Type": "application/json"}
    if channel == "TELEGRAM":
        url = "https://api.telegram.org/bot" + env[prefix + "TOKEN"] + "/sendMessage"
        body = {"chat_id": env[prefix + "CHAT_ID"], "text": text}
        limit = 4096
    elif channel == "DISCORD":
        parts = urlsplit(env[prefix + "URL"])
        query = [(k, v) for k, v in parse_qsl(parts.query) if k != "wait"] + [("wait", "true")]
        url = urlunsplit(parts._replace(query=urlencode(query)))
        body = {"content": text, "allowed_mentions": {"parse": []}}
        limit = 2000
    elif channel == "LINE":
        url = "https://api.line.me/v2/bot/message/push"
        headers["Authorization"] = "Bearer " + env[prefix + "TOKEN"]
        body = {"to": env[prefix + "TO"], "messages": [{"type": "text", "text": text}]}
        limit = 5000
    else:
        url = env[prefix + "URL"]
        if env.get(prefix + "TOKEN"):
            headers["Authorization"] = "Bearer " + env[prefix + "TOKEN"]
        headers["Idempotency-Key"] = identity
        body = {"alert_identity": identity, "message": text, "alert": deepcopy(packet)}
        limit = 100000
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return None, "INVALID_CHANNEL_URL"
    # Never silently truncate risk/entry fields or send a partially delivered multipart alert.
    if len(text.encode("utf-16-le")) // 2 > limit:
        return None, "MESSAGE_TOO_LONG"
    return (url, headers, body), None


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_transport(url, headers, body):
    request = Request(url, data=canonical(body).encode(), headers=headers, method="POST")
    with build_opener(NoRedirect()).open(request, timeout=5) as response:
        payload = response.read(65536)
        return response.status, payload


def read_journal(path):
    if not path.exists():
        return {"schema": SCHEMA, "records": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != SCHEMA or not isinstance(data.get("records"), list):
        raise ValueError("INVALID_JOURNAL")
    return data


def deliver(packet, path=DEFAULT_JOURNAL, *, env=None, transport=None, clock=time.time):
    """Fail-open to caller, fail-closed to sending when durable dedupe is unavailable."""
    env = os.environ if env is None else env
    transport = http_transport if transport is None else transport
    path = Path(path)
    lock = path.with_suffix(path.suffix + ".lock")
    owned = False
    try:
        if packet.get("schema") != "opportunity_alert.v1" or packet.get("review_state") not in LEVELS:
            return {"status": "SKIPPED_INVALID_PACKET"}
        expected = LEVELS[packet["review_state"]]
        if expected == "SILENT":
            return {"status": "SILENT"}
        if packet.get("alert_level") != expected or packet.get("emit_alert") is not True:
            return {"status": "SKIPPED_INVALID_PACKET"}
        if not isinstance(packet.get("generated_at"), int) or packet["generated_at"] <= 0:
            return {"status": "SKIPPED_INVALID_PACKET"}
        identity = alert_identity(packet)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.close(fd)
        owned = True
        journal = read_journal(path)
        records = journal["records"]
        results = []
        for channel in CHANNELS:
            row = {"alert_identity": identity, "channel": channel, "attempted_at": int(clock()),
                   "status": None, "response_code": None, "error": None, "delivered_at": None}
            attempted = any(r["alert_identity"] == identity and r["channel"] == channel and
                            r["status"] in {"ATTEMPTED", "DELIVERED", "DELIVERY_FAILED"} for r in records)
            request, reason = adapter_request(channel, env, packet, identity)
            if attempted:
                row["status"] = "DUPLICATE_SUPPRESSED"
            elif reason:
                row["status"] = reason if reason.startswith("SKIPPED_") else "DELIVERY_FAILED"
                row["error"] = None if reason.startswith("SKIPPED_") else reason
            else:
                row["status"] = "ATTEMPTED"
                records.append(row)
                atomic_write_json(path, journal)
                try:
                    code, response = transport(*request)
                    row["response_code"] = int(code)
                    ok = 200 <= code < 300
                    if ok and channel == "TELEGRAM":
                        ok = json.loads(response).get("ok") is True
                    row["status"] = "DELIVERED" if ok else "DELIVERY_FAILED"
                    row["error"] = None if ok else "HTTP_OR_PROVIDER_ERROR"
                    row["delivered_at"] = int(clock()) if ok else None
                except HTTPError as exc:
                    row.update(status="DELIVERY_FAILED", response_code=exc.code, error="HTTP_ERROR")
                except TimeoutError:
                    row.update(status="DELIVERY_FAILED", error="TIMEOUT")
                except Exception:
                    row.update(status="DELIVERY_FAILED", error="TRANSPORT_ERROR")
                atomic_write_json(path, journal)
                results.append(deepcopy(row))
                continue
            records.append(row)
            atomic_write_json(path, journal)
            results.append(deepcopy(row))
        return {"status": "PROCESSED", "records": results}
    except Exception:
        return {"status": "DELIVERY_FAILED", "error": "JOURNAL_OR_ROUTER_UNAVAILABLE"}
    finally:
        if owned:
            try:
                lock.unlink()
            except OSError:
                pass


def dispatch_completed_reviews(reviews, path):
    try:
        for review in reviews.values():
            packet = review.get("opportunity_alert")
            if isinstance(packet, dict):
                deliver(packet, path)
    except Exception:
        pass  # Notification failures never enter the observation recovery transaction.


def notification_status(path=DEFAULT_JOURNAL, env=None):
    env = os.environ if env is None else env
    result = {"schema": SCHEMA, "enabled_channels": [c for c in CHANNELS if
              env.get("GRIM_NOTIFY_" + c + "_ENABLED", "").lower() == "true"]}
    try:
        rows = read_journal(Path(path))["records"]
        result.update(last_delivery=next((r for r in reversed(rows) if r["status"] == "DELIVERED"), None),
                      failed_deliveries=sum(r["status"] == "DELIVERY_FAILED" for r in rows),
                      skipped_deliveries=sum(r["status"].startswith("SKIPPED_") for r in rows),
                      duplicate_suppressed_count=sum(r["status"] == "DUPLICATE_SUPPRESSED" for r in rows),
                      uncertain_attempts=sum(r["status"] == "ATTEMPTED" for r in rows))
    except Exception:
        result["error"] = "JOURNAL_UNAVAILABLE"
    return result
