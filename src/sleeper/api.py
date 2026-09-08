"""HTTP access to Sleeper, ESPN and FantasyPros. Every request passes through the rate limiter."""
from __future__ import annotations

import math
import os
import time
from typing import Any

import httpx

from . import __version__, ratelimit
from .ratelimit import CACHE_DIR  # noqa: F401  (re-exported for players/rankings)

HOSTS = {
    "sleeper": "https://api.sleeper.app/v1",
    "sleeper_root": "https://api.sleeper.app",
    "fantasypros": "https://api.fantasypros.com/public/v2/json",
    "espn": "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl",
}
# Hosts that draw from another host's request budget.
LIMITER_OF = {"sleeper_root": "sleeper"}
TIMEOUT = 10.0
DEFAULT_BLOCK = 60.0
MAX_BLOCK = 300.0


class SleeperError(Exception):
    """Any failure the CLI reports as a one-line message."""


class NotFound(SleeperError):
    def __init__(self, path: str):
        super().__init__(f"not found: {path}")
        self.path = path


class TransientError(SleeperError):
    """Network-level or 5xx failure; the draft poll retries these."""


class RateLimited(SleeperError):
    def __init__(self, host: str, seconds: float):
        super().__init__(f"{host} returned 429; paused for {seconds:.0f} s")
        self.host = host
        self.seconds = seconds


_client: httpx.Client | None = None


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=TIMEOUT, headers={"User-Agent": f"sleeper-cli/{__version__}"})
    return _client


def get_response(
    host: str,
    path: str,
    params: dict | None = None,
    fresh: bool = False,
    headers: dict | None = None,
) -> httpx.Response:
    base = HOSTS[host]
    params = dict(params or {})
    if fresh:
        params["_"] = int(time.time() * 1000)
    headers = dict(headers or {})
    if host == "fantasypros":
        key = os.environ.get("FANTASYPROS_API_KEY")
        if not key:
            raise SleeperError("FANTASYPROS_API_KEY is not set (copy .env.example to .env and fill it in)")
        headers["x-api-key"] = key
    ratelimit.acquire(LIMITER_OF.get(host, host))
    url = f"{base}/{path.lstrip('/')}"
    try:
        r = client().get(url, params=params, headers=headers)
    except httpx.HTTPError as e:
        raise TransientError(f"{host} {path}: {type(e).__name__}") from e
    if r.status_code == 429:
        seconds = _retry_after(r)
        ratelimit.block(LIMITER_OF.get(host, host), seconds)
        raise RateLimited(host, seconds)
    if r.status_code == 404:
        raise NotFound(path)
    if r.status_code >= 500 or 300 <= r.status_code < 400:
        raise TransientError(f"{host} {path}: HTTP {r.status_code}")
    if r.status_code >= 400:
        raise SleeperError(f"{host} {path}: HTTP {r.status_code}: {r.text[:200]}")
    return r


def get(host: str, path: str, params: dict | None = None, fresh: bool = False, headers: dict | None = None) -> Any:
    r = get_response(host, path, params=params, fresh=fresh, headers=headers)
    try:
        data = r.json()
    except ValueError as e:
        raise TransientError(f"{host} {path}: non-JSON body (HTTP {r.status_code})") from e
    if data is None:
        raise NotFound(path)
    return data


def _retry_after(r: httpx.Response) -> float:
    try:
        v = float(r.headers.get("Retry-After", ""))
    except ValueError:
        return DEFAULT_BLOCK
    return min(max(1.0, v), MAX_BLOCK) if math.isfinite(v) else DEFAULT_BLOCK
