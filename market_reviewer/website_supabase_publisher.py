"""Manual hosted read-store publisher. No Runner hooks or credential persistence."""
from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .website_public_snapshot import build_public_snapshot, validate_public_snapshot
from .website_read_model import build_website_read_model

URL_ENV = "GRIM_WEBSITE_SUPABASE_URL"
KEY_ENV = "GRIM_WEBSITE_SUPABASE_SERVICE_ROLE_KEY"
TABLE_PATH = "/rest/v1/grim_website_snapshot"
READ_PATH = TABLE_PATH + "?id=eq.latest&select=snapshot,generated_at,source_observation,stale_after_seconds,updated_at"
TIMEOUT_SECONDS = 10


def _valid_url(value: str) -> bool:
    try:
        url = urlsplit(value)
        return bool(value and not re.search(r"\s", value) and
                    url.scheme == "https" and url.hostname and
                    re.fullmatch(r"[a-zA-Z0-9.-]+", url.hostname) and
                    "." in url.hostname and not url.username and not url.password and
                    url.port in (None, 443) and url.path in ("", "/") and
                    not url.query and not url.fragment)
    except (ValueError, TypeError):
        return False


def supabase_status() -> dict:
    """Configuration presence only; no network, filesystem writes or values."""
    url, key = os.environ.get(URL_ENV, ""), os.environ.get(KEY_ENV, "")
    configured = bool(key.strip())
    valid_key = configured and not any(c.isspace() for c in key)
    valid_url = _valid_url(url)
    return {"url_configured": bool(url), "credential_configured": configured,
            "url_valid_https": valid_url,
            "ready": valid_url and valid_key}


def remote_read_url(project_url: str) -> str:
    """Read contract only. Consumers use public/anon credentials, never service key."""
    if not _valid_url(project_url):
        raise ValueError("INVALID_HTTPS_PROJECT_URL")
    return project_url.rstrip("/") + READ_PATH


class PublishError(Exception):
    def __init__(self, code: str, http_status: int | None = None):
        super().__init__(code)
        self.code = code
        self.http_status = http_status


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SupabasePublisher:
    """Single HTTPS upsert attempt; URL/key are read only from environment."""

    def publish(self, snapshot: dict) -> None:
        try:
            validate_public_snapshot(snapshot)
        except Exception:
            raise PublishError("INVALID_PUBLIC_SNAPSHOT") from None
        if not supabase_status()["ready"]:
            raise PublishError("NOT_CONFIGURED_OR_INVALID")
        url = os.environ[URL_ENV].rstrip("/")
        key = os.environ[KEY_ENV]
        payload = {"id": "latest", **{k: snapshot[k] for k in (
            "schema", "generated_at", "source_observation", "stale_after_seconds")},
                   "snapshot": snapshot,
                   "updated_at": datetime.fromtimestamp(snapshot["generated_at"], timezone.utc).isoformat()}
        headers = {"Content-Type": "application/json", "apikey": key,
                   "Prefer": "resolution=merge-duplicates,return=minimal",
                   "Content-Profile": "public"}
        # Legacy service_role JWTs use Bearer as well as apikey. New secret API
        # keys are not JWTs and must not be sent as Authorization Bearer.
        if not key.startswith("sb_secret_"):
            headers["Authorization"] = "Bearer " + key
        try:
            request = Request(url + TABLE_PATH + "?on_conflict=id",
                              data=json.dumps(payload, sort_keys=True, allow_nan=False).encode("utf-8"),
                              headers=headers, method="POST")
            # Never forward privileged headers to redirect targets or implicit proxies.
            opener = build_opener(ProxyHandler({}), _NoRedirect())
            with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
                code = response.status
                if not 200 <= code < 300:
                    raise PublishError("HTTP_ERROR", code)
        except HTTPError as exc:
            code = exc.code
            exc.close()
            raise PublishError("HTTP_ERROR", code) from None
        except TimeoutError:
            raise PublishError("TIMEOUT") from None
        except URLError:
            raise PublishError("NETWORK_ERROR") from None
        except PublishError:
            raise
        except Exception:
            raise PublishError("TRANSPORT_ERROR") from None


def publish_supabase(*, builder=None, now: int | None = None) -> dict:
    """Fail-open manual orchestration; no local writes and no exception details."""
    if not supabase_status()["ready"]:
        return {"status": "PUBLISH_FAILED", "error": "NOT_CONFIGURED_OR_INVALID", "http_status": None}
    try:
        snapshot = build_public_snapshot((builder or build_website_read_model)(), now=now)
        SupabasePublisher().publish(snapshot)
        return {"status": "PUBLISHED", "error": None, "http_status": None}
    except PublishError as exc:
        return {"status": "PUBLISH_FAILED", "error": exc.code, "http_status": exc.http_status}
    except Exception:
        return {"status": "PUBLISH_FAILED", "error": "SNAPSHOT_BUILD_FAILED", "http_status": None}
