"""Phase 4 tests — the Redis circuit breaker.

These target the *strict-mode* Redis path (the path a deployed replica takes),
which the Phase-0 suite doesn't cover because it runs in memory-fallback mode.

The point of the breaker is to stop the Phase-4 stall: during a Redis outage a
sync `/segment` worker must NOT keep blocking on a dead Redis. Once the breaker
opens, reads/writes skip Redis entirely and fail fast to origin — so we assert
(a) it opens after N failures, (b) it then stops calling Redis, (c) the outage
stays visible (redis_up=0, every request a MISS), and (d) it self-heals.
"""

import os

# Keep the simulated origin fast. NOTE: we do NOT set CACHE_MEMORY_FALLBACK here —
# every Cache in this module is built with allow_fallback=False explicitly, so the
# strict Redis path is exercised regardless of env, and we avoid mutating a
# process-wide var that app.main (imported by the other test module) reads at import
# time — which would make the suite order-dependent.
os.environ.setdefault("ORIGIN_LATENCY_SECONDS", "0.01")

import time  # noqa: E402

from app import metrics  # noqa: E402
from app.cache import Cache, _CircuitBreaker  # noqa: E402


class FakeRedis:
    """Minimal Redis stand-in whose failure can be toggled at runtime."""

    def __init__(self, fail: bool = True) -> None:
        self.fail = fail
        self.calls = 0
        self.store: dict[str, bytes] = {}

    def get(self, key):
        self.calls += 1
        if self.fail:
            raise ConnectionError("redis down")
        return self.store.get(key)

    def set(self, key, value, ex=None):
        self.calls += 1
        if self.fail:
            raise ConnectionError("redis down")
        self.store[key] = value

    def ping(self):
        if self.fail:
            raise ConnectionError("redis down")
        return True

    def flushdb(self):
        self.store.clear()


def _strict_cache(fake: FakeRedis, threshold: int = 3, cooldown: float = 0.2) -> Cache:
    """A strict-mode Cache wired to a fake Redis and a fast-tuned breaker."""
    c = Cache(allow_fallback=False)
    c._client = fake
    c._breaker = _CircuitBreaker(threshold=threshold, cooldown=cooldown)
    return c


def _gauge(g) -> float:
    return g._value.get()


def test_breaker_opens_after_threshold_then_stops_calling_redis():
    """After `threshold` failures the breaker OPENS and Redis is not called again."""
    fake = FakeRedis(fail=True)
    c = _strict_cache(fake, threshold=3, cooldown=5.0)

    # First request: read fails (1), write fails (2) → still closed.
    c.get_segment("a")
    assert c._breaker.state == "closed"
    assert fake.calls == 2

    # Second request: read fails (3) → OPENS; the write is then skipped.
    c.get_segment("b")
    assert c._breaker.state == "open"
    assert fake.calls == 3  # the opening read, no write attempted

    # Third+ requests while open: ZERO further Redis calls (this is the fix — no
    # more worker threads stalling on a dead dependency).
    calls_before = fake.calls
    for i in range(5):
        r = c.get_segment(f"c{i}")
        assert r.cache_hit is False  # always a miss → origin
    assert fake.calls == calls_before


def test_outage_stays_visible_while_open():
    """An open breaker drops the stall, not the signal: redis_up=0, all misses."""
    fake = FakeRedis(fail=True)
    c = _strict_cache(fake, threshold=2, cooldown=5.0)

    for i in range(4):
        res = c.get_segment(f"seg{i}")
        assert res.cache_hit is False

    assert c._breaker.state == "open"
    assert _gauge(metrics.redis_up) == 0            # EdgeCacheRedisDown still fires
    assert _gauge(metrics.redis_circuit_state) == 2  # dashboard shows the breaker open


def test_breaker_half_open_probe_recovers():
    """Once Redis is back, the half-open probe closes the breaker and caching resumes."""
    fake = FakeRedis(fail=True)
    c = _strict_cache(fake, threshold=2, cooldown=0.2)

    # Trip it open.
    c.get_segment("x")
    c.get_segment("x")
    assert c._breaker.state == "open"

    # Redis recovers; wait out the cooldown so the next call is a half-open probe.
    fake.fail = False
    time.sleep(0.25)

    # First post-recovery request: half-open probe succeeds → breaker closes, and
    # the fetched segment is cached again.
    c.get_segment("x")
    assert c._breaker.state == "closed"
    assert _gauge(metrics.redis_circuit_state) == 0

    # Cache is live again: a warmed key now reads back as a HIT.
    c.get_segment("warm")            # miss + populate
    assert c.get_segment("warm").cache_hit is True
    assert _gauge(metrics.redis_up) == 1


def test_half_open_failure_reopens_and_restarts_cooldown():
    """A failed half-open probe re-opens the breaker instead of flapping closed."""
    fake = FakeRedis(fail=True)
    c = _strict_cache(fake, threshold=2, cooldown=0.2)

    c.get_segment("x")
    c.get_segment("x")
    assert c._breaker.state == "open"

    time.sleep(0.25)                 # cooldown elapsed → next call is a probe
    calls_before = fake.calls
    c.get_segment("y")               # probe: read attempted once, still failing
    assert fake.calls == calls_before + 1   # exactly ONE probe call
    assert c._breaker.state == "open"        # probe failed → re-opened


def test_breaker_can_be_disabled():
    """With the breaker disabled, every request keeps hitting (dead) Redis — the
    pre-fix behaviour, kept as an env switch for the before/after demo."""
    fake = FakeRedis(fail=True)
    c = Cache(allow_fallback=False)
    c._client = fake
    c._cb_enabled = False

    for i in range(5):
        c.get_segment(f"z{i}")
    # No breaker → read is attempted every single request (the stall path).
    assert fake.calls >= 5
