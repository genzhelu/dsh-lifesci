"""Unit tests for :mod:`lifesci.ratelimit` — token buckets, cooldowns, backoff."""

from __future__ import annotations

import pytest

from lifesci.ratelimit import HostLimiter, TokenBucket, backoff_seconds, hostname

from tests.conftest import FakeClock


# ---------------------------------------------------------------------------
# token bucket
# ---------------------------------------------------------------------------


def test_token_bucket_allows_burst_then_waits_for_refill():
    clock = FakeClock()
    limiter = HostLimiter(
        limits={"api.example.org": {"rate": 2.0, "burst": 2.0}},
        clock=clock,
        sleeper=clock.sleep,
    )
    assert limiter.wait("https://api.example.org/x") == 0.0
    assert limiter.wait("https://api.example.org/y") == 0.0
    # Burst exhausted; one token takes 1/rate = 0.5 s to refill.
    waited = limiter.wait("https://api.example.org/z")
    assert waited == pytest.approx(0.5)
    assert clock.sleeps == [0.5]
    # wait() consumes exactly the one token it refilled, so the bucket is empty
    # again and the next call must wait another 0.5 s (the clock has not moved).
    assert limiter.wait("api.example.org") == pytest.approx(0.5)
    assert clock.sleeps == [0.5, 0.5]
    # Advancing the clock by a full refill period makes a call free again.
    clock.now += 1.0
    assert limiter.wait("api.example.org") == 0.0


def test_token_bucket_try_acquire_returns_seconds_to_wait():
    bucket = TokenBucket(rate=1.0, burst=1.0)
    assert bucket.try_acquire(0.0) == 0.0
    assert bucket.try_acquire(0.0) == pytest.approx(1.0)
    assert bucket.try_acquire(0.5) == pytest.approx(0.5)
    assert bucket.try_acquire(1.0) == pytest.approx(0.0)


def test_limiter_is_per_host_and_defaults_to_generic_config():
    clock = FakeClock()
    limiter = HostLimiter(
        limits={
            "a.example.org": {"rate": 1.0, "burst": 1.0},
            "default": {"rate": 2.0, "burst": 2.0},
        },
        clock=clock,
        sleeper=clock.sleep,
    )
    assert limiter.wait("https://a.example.org/1") == 0.0
    # A different host is unaffected by a's exhausted bucket.
    assert limiter.wait("https://b.example.org/1") == 0.0
    # a is throttled...
    assert limiter.wait("https://a.example.org/2") == pytest.approx(1.0)
    # ...while b still has one default-burst token left.
    assert limiter.wait("https://b.example.org/2") == 0.0
    status = limiter.status()
    assert set(status) == {"a.example.org", "b.example.org"}
    assert status["a.example.org"]["rate"] == 1.0
    assert status["a.example.org"]["waited_s"] == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# penalties / cooldowns
# ---------------------------------------------------------------------------


def test_penalise_applies_host_wide_cooldown():
    clock = FakeClock()
    limiter = HostLimiter(
        limits={"default": {"rate": 100.0, "burst": 100.0}},
        clock=clock,
        sleeper=clock.sleep,
    )
    limiter.penalise("https://portal.example.org/a", 5.0)
    waited = limiter.wait("https://portal.example.org/b")  # different path, same host
    assert waited == pytest.approx(5.0)
    assert clock.sleeps == [5.0]
    assert limiter.status()["portal.example.org"]["cooldown_s"] == 0.0


def test_penalise_never_shortens_existing_cooldown():
    clock = FakeClock()
    limiter = HostLimiter(limits={"default": {"rate": 100.0, "burst": 100.0}}, clock=clock, sleeper=clock.sleep)
    limiter.penalise("h.example.org", 10.0)
    limiter.penalise("h.example.org", 1.0)  # shorter penalty must not reduce it
    assert limiter.wait("h.example.org") == pytest.approx(10.0)


def test_penalise_does_not_affect_other_hosts():
    clock = FakeClock()
    limiter = HostLimiter(limits={"default": {"rate": 100.0, "burst": 100.0}}, clock=clock, sleeper=clock.sleep)
    limiter.penalise("slow.example.org", 30.0)
    assert limiter.wait("fast.example.org") == 0.0


# ---------------------------------------------------------------------------
# hostname normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://Example.ORG/path?q=1", "example.org"),
        ("https://user:pass@example.org:8443/x", "example.org"),
        ("example.org", "example.org"),
        ("  EXAMPLE.ORG:443  ", "example.org"),
    ],
)
def test_hostname_normalisation(raw, expected):
    assert hostname(raw) == expected


# ---------------------------------------------------------------------------
# backoff
# ---------------------------------------------------------------------------


def test_backoff_seconds_is_seeded_deterministic():
    # Same seed -> identical value (tests must never flake on jitter).
    for attempt in (1, 2, 3, 4, 10):
        assert backoff_seconds(attempt, base=1.0, maximum=30.0, seed=7) == backoff_seconds(
            attempt, base=1.0, maximum=30.0, seed=7
        )


def test_backoff_seconds_without_jitter_is_exponential():
    assert backoff_seconds(1, base=1.0, maximum=30.0, jitter=0.0) == 1.0
    assert backoff_seconds(2, base=1.0, maximum=30.0, jitter=0.0) == 2.0
    assert backoff_seconds(3, base=2.0, maximum=30.0, jitter=0.0) == 8.0


def test_backoff_seconds_is_capped_and_bounded():
    for attempt in range(1, 25):
        value = backoff_seconds(attempt, base=1.0, maximum=30.0, seed=attempt)
        assert 0.0 <= value <= 30.0
    assert backoff_seconds(99, base=1.0, maximum=30.0, jitter=0.0) == 30.0


def test_backoff_seconds_jitter_stays_within_bounds():
    base, jitter = 4.0, 0.25
    # backoff is exponential: attempt 3 -> raw = base * 2**(3-1) = 16 s.
    raw = base * (2 ** (3 - 1))
    for seed in range(50):
        value = backoff_seconds(3, base=base, maximum=100.0, jitter=jitter, seed=seed)
        assert raw * (1 - jitter) <= value <= raw * (1 + jitter)
    # attempt 0/negative is normalised to attempt 1 (no zero-length backoff).
    assert backoff_seconds(0, base=2.0, maximum=30.0, jitter=0.0) == 2.0
