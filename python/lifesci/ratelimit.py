"""Polite per-host rate limiting (token buckets with penalty/cooldown support).

The HTTP layer calls :meth:`HostLimiter.wait` before every network attempt and
:meth:`HostLimiter.penalise` when a portal answers 429/503.  Limiter instances
are per-command (attached to the :class:`lifesci.context.Context`), so separate
commands cannot starve each other and tests can inject a fake clock/sleeper.

Defaults are deliberately conservative: public scientific portals are shared
infrastructure and repeated 429/503 answers mean we must back off, not hammer.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping
from urllib.parse import urlsplit

from .context import DEFAULT_RATE_LIMITS


@dataclass
class TokenBucket:
    """Classic token bucket: ``burst`` tokens, refilled at ``rate`` tokens/second."""

    rate: float
    burst: float
    tokens: float | None = None
    last_refill: float | None = None

    def _refill(self, now: float) -> None:
        if self.last_refill is None:
            self.last_refill = now
            if self.tokens is None:
                self.tokens = self.burst
            return
        elapsed = max(0.0, now - self.last_refill)
        self.last_refill = now
        if self.tokens is None:
            self.tokens = self.burst
        self.tokens = min(self.burst, self.tokens + elapsed * self.rate)

    def try_acquire(self, now: float, amount: float = 1.0) -> float:
        """Return 0.0 when a token is available, else seconds to wait."""
        self._refill(now)
        assert self.tokens is not None
        if self.tokens >= amount:
            self.tokens -= amount
            return 0.0
        return (amount - self.tokens) / self.rate


@dataclass
class HostLimiter:
    """Thread-safe limiter registry keyed by hostname."""

    limits: Mapping[str, Mapping[str, float]] = field(
        default_factory=lambda: dict(DEFAULT_RATE_LIMITS)
    )
    clock: Callable[[], float] = time.monotonic
    sleeper: Callable[[float], None] = time.sleep
    buckets: dict[str, TokenBucket] = field(default_factory=dict)
    cooldown_until: dict[str, float] = field(default_factory=dict)
    waits_total: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # ------------------------------------------------------------------
    def _config_for(self, host: str) -> tuple[float, float]:
        cfg = self.limits.get(host) or self.limits.get("default") or {"rate": 2.0, "burst": 4.0}
        return float(cfg.get("rate", 2.0)), float(cfg.get("burst", 4.0))

    def _bucket_for(self, host: str) -> TokenBucket:
        bucket = self.buckets.get(host)
        if bucket is None:
            rate, burst = self._config_for(host)
            bucket = TokenBucket(rate=max(rate, 1e-6), burst=max(burst, 1.0))
            self.buckets[host] = bucket
        return bucket

    # ------------------------------------------------------------------
    def wait(self, url_or_host: str) -> float:
        """Block until a request to this host is allowed; returns seconds waited.

        Loops until a token has actually been acquired: under multi-thread
        contention another waiter may consume the refilled token while we sleep,
        so the token must be charged inside the same critical section that
        observes it available.
        """
        host = hostname(url_or_host)
        waited = 0.0
        while True:
            with self._lock:
                bucket = self._bucket_for(host)
                now = self.clock()
                wait_for = bucket.try_acquire(now)  # charges a token when available
                cooldown = self.cooldown_until.get(host, 0.0) - now
                wait_for = max(wait_for, cooldown, 0.0)
            if wait_for <= 0:
                break
            if waited > 300.0:  # pragma: no cover - pathological clock/limit config
                from .errors import ConfigError

                raise ConfigError(
                    f"rate limiter waited more than 300 s for {host}; check the rate limit configuration",
                    details={"host": host, "waited_s": round(waited, 2)},
                )
            self.sleeper(wait_for)
            waited += wait_for
        with self._lock:
            self.waits_total[host] = self.waits_total.get(host, 0.0) + waited
        return waited

    def penalise(self, url_or_host: str, seconds: float) -> None:
        """Apply a host-wide cooldown after a 429/503 response."""
        host = hostname(url_or_host)
        with self._lock:
            until = self.clock() + max(0.0, seconds)
            self.cooldown_until[host] = max(self.cooldown_until.get(host, 0.0), until)

    def status(self) -> dict[str, dict[str, float]]:
        with self._lock:
            now = self.clock()
            return {
                host: {
                    "rate": bucket.rate,
                    "burst": bucket.burst,
                    "tokens": round(bucket.tokens if bucket.tokens is not None else bucket.burst, 3),
                    "cooldown_s": round(max(0.0, self.cooldown_until.get(host, 0.0) - now), 2),
                    "waited_s": round(self.waits_total.get(host, 0.0), 2),
                }
                for host, bucket in self.buckets.items()
            }


def hostname(url_or_host: str) -> str:
    """Normalise a URL or bare hostname to a lower-case hostname key."""
    candidate = url_or_host.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        parsed = urlsplit(candidate)
        return (parsed.hostname or candidate).lower()
    except ValueError:  # pragma: no cover - malformed input
        return candidate.lower()


def backoff_seconds(
    attempt: int,
    *,
    base: float = 1.0,
    maximum: float = 30.0,
    jitter: float = 0.25,
    seed: int | None = None,
) -> float:
    """Exponential backoff with bounded jitter (attempt is 1-based)."""
    import random

    rng = random.Random(seed)
    raw = min(maximum, base * (2 ** (max(1, attempt) - 1)))
    factor = 1.0 + rng.uniform(-jitter, jitter)
    return max(0.0, min(maximum, raw * factor))
