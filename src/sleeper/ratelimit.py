"""Per-host request caps shared across processes.

Each host's state is a small JSON file under CACHE_DIR guarded by an exclusive
flock, so parallel CLI invocations draw from one budget.
"""
from __future__ import annotations

import fcntl
import json
import math
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

CACHE_DIR = Path.home() / ".cache" / "sleeper"
CAPS = {"sleeper": 500, "fantasypros": 1, "espn": 5}
WINDOW = 60.0
NOTIFY_AFTER = 2.0


class RateLimiter:
    def __init__(
        self,
        host: str,
        cap: int | None = None,
        state_dir: Path | None = None,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        notify: Callable[[str], None] | None = None,
    ):
        self.host = host
        self.cap = CAPS[host] if cap is None else cap
        self.state_dir = state_dir or CACHE_DIR
        self.now = now
        self.sleep = sleep
        self.notify = notify or (lambda msg: print(msg, file=sys.stderr))
        self._state = self.state_dir / f"ratelimit.{host}.json"
        self._lock = self.state_dir / f"ratelimit.{host}.lock"

    @contextmanager
    def _locked(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with open(self._lock, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                yield self._read()
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)

    def _read(self) -> dict:
        try:
            st = json.loads(self._state.read_text())
            sent = [float(t) for t in (st.get("sent") or []) if isinstance(t, (int, float))]
            blocked = float(st.get("blocked_until") or 0)
            return {"sent": sent, "blocked_until": blocked if math.isfinite(blocked) else 0.0}
        except (OSError, ValueError, TypeError, AttributeError):
            return {"sent": [], "blocked_until": 0.0}

    def _write(self, st: dict) -> None:
        tmp = self._state.with_suffix(".tmp")
        tmp.write_text(json.dumps(st))
        tmp.replace(self._state)

    def acquire(self) -> None:
        """Block until a request is allowed, then record it."""
        while True:
            with self._locked() as st:
                now = self.now()
                sent = [t for t in st["sent"] if now - t < WINDOW]
                why = None
                if st["blocked_until"] > now:
                    wait = st["blocked_until"] - now
                    why = f"paused after a 429 from {self.host}"
                elif len(sent) >= self.cap:
                    wait = sent[0] + WINDOW - now
                    why = f"rate limit ({self.host} {self.cap}/min)"
                else:
                    wait = 0.0
                    sent.append(now)
                self._write({"sent": sent, "blocked_until": st["blocked_until"]})
            if wait <= 0:
                return
            if wait > NOTIFY_AFTER:
                self.notify(f"{why}: waiting {wait:.0f} s")
            self.sleep(wait)

    def block(self, seconds: float) -> None:
        """Pause every process using this host for `seconds` (after a 429)."""
        with self._locked() as st:
            st["blocked_until"] = max(st["blocked_until"], self.now() + seconds)
            self._write(st)

    def status(self) -> dict:
        with self._locked() as st:
            now = self.now()
            return {
                "host": self.host,
                "cap_per_min": self.cap,
                "sent_last_minute": sum(1 for t in st["sent"] if now - t < WINDOW),
                "paused_for_s": max(0, round(st["blocked_until"] - now)),
            }


_limiters: dict[str, RateLimiter] = {}


def limiter(host: str) -> RateLimiter:
    if host not in _limiters:
        _limiters[host] = RateLimiter(host)
    return _limiters[host]


def acquire(host: str) -> None:
    limiter(host).acquire()


def block(host: str, seconds: float) -> None:
    limiter(host).block(seconds)
