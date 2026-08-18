"""Connectivity helpers kept separate from review-only code."""

from __future__ import annotations

from urllib.request import Request, urlopen


def can_reach(url: str, timeout: float = 5.0) -> bool:
    request = Request(url, method="HEAD")
    try:
        with urlopen(request, timeout=timeout) as response:
            return 200 <= response.status < 500
    except OSError:
        return False
