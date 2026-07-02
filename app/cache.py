"""Redis cache layer for the edge-cache service.

Wraps a Redis client and adds a tiny "origin fetch" simulator. The origin is the
hypothetical source of truth for a video segment; on a cache miss we pay a small
latency penalty (like fetching from a far-away origin) and then populate the cache
so subsequent reads are fast hits.

Two operating modes, chosen by CACHE_MEMORY_FALLBACK (default "false"):

- **Strict (cluster/production, default).** Redis is a hard dependency. The client
  connects lazily and *auto-reconnects* (redis-py reconnects on the next command),
  so a brief startup race with Redis self-heals on the following request. If Redis
  is genuinely down, reads are treated as cache-unavailable → forced origin fetch,
  and we surface that on the `redis_up` gauge + `redis_errors_total` counter. This
  is what makes a Redis outage *visible* (latency up, hit ratio collapses) — exactly
  the incident signal Phase 4 relies on. We never silently mask an outage with a
  local dict here.

- **Fallback (tests / local without a Redis server).** If a real Redis can't be
  reached at startup we use an in-process dict so unit tests stay hermetic and fast.

The Redis URL comes from REDIS_URL so the same code runs under docker-compose
(redis://redis:6379/0) and Kubernetes (in-cluster hostname).
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from typing import Optional

from . import metrics

try:  # redis is optional at import time so tests can run without it installed
    import redis as _redis
except Exception:  # pragma: no cover - exercised only when redis is absent
    _redis = None


SEGMENT_TTL_SECONDS = int(os.getenv("SEGMENT_TTL_SECONDS", "60"))
ORIGIN_LATENCY_SECONDS = float(os.getenv("ORIGIN_LATENCY_SECONDS", "0.15"))
SEGMENT_SIZE_BYTES = int(os.getenv("SEGMENT_SIZE_BYTES", "4096"))

# --- Circuit-breaker tuning (guards the Redis dependency) ---
# Open after this many consecutive Redis failures; while open, skip Redis for the
# cooldown window, then allow a single half-open probe to test recovery.
CB_FAIL_THRESHOLD = int(os.getenv("REDIS_CB_FAIL_THRESHOLD", "3"))
CB_COOLDOWN_SECONDS = float(os.getenv("REDIS_CB_COOLDOWN_SECONDS", "5.0"))

# Breaker state names + their numeric encoding for the redis_circuit_state gauge.
_CB_CLOSED = "closed"
_CB_HALF_OPEN = "half_open"
_CB_OPEN = "open"
_CB_STATE_VALUE = {_CB_CLOSED: 0, _CB_HALF_OPEN: 1, _CB_OPEN: 2}


def _fallback_allowed() -> bool:
    return os.getenv("CACHE_MEMORY_FALLBACK", "false").lower() in ("1", "true", "yes")


def _cb_enabled() -> bool:
    return os.getenv("REDIS_CB_ENABLED", "true").lower() in ("1", "true", "yes")


class _CircuitBreaker:
    """Trip-and-cooldown breaker in front of the Redis dependency.

    The Phase-4 stall was this: `/segment` is a *sync* endpoint, so every request
    runs on a worker thread. During a Redis outage each request blocked on a dead
    Redis connection, worker threads piled up faster than they drained, throughput
    collapsed, and the hit/miss counters barely advanced — so the 5-minute
    hit-ratio average never crossed the alert threshold. The breaker stops calling
    a dependency we already know is down, so requests fail *fast* to origin instead.

    States:
      CLOSED     normal — Redis calls allowed.
      OPEN       Redis failed >= threshold times in a row → skip Redis entirely
                 (straight to origin) for the cooldown window. This is what frees
                 the worker threads and lets the hit ratio collapse cleanly.
      HALF_OPEN  cooldown elapsed → allow exactly ONE probe. Success closes the
                 breaker; failure re-opens it and restarts the cooldown.

    Thread-safe: sync endpoints run in a thread pool, so many workers touch this
    concurrently. Every transition is published to the redis_circuit_state gauge.

    IMPORTANT: an open breaker never masks the outage. Callers still report
    redis_up=0 when they skip Redis, so EdgeCacheRedisDown keeps firing — we trade
    the *stall*, not the *visibility*.
    """

    def __init__(self, threshold: int = CB_FAIL_THRESHOLD, cooldown: float = CB_COOLDOWN_SECONDS) -> None:
        self._threshold = max(1, threshold)
        self._cooldown = max(0.0, cooldown)
        self._lock = threading.Lock()
        self._state = _CB_CLOSED
        self._failures = 0
        self._opened_at = 0.0
        self._half_open_pending = False  # a probe is in flight; hold other workers back
        self._publish()

    def _publish(self) -> None:
        metrics.redis_circuit_state.set(_CB_STATE_VALUE[self._state])

    @property
    def state(self) -> str:
        return self._state

    def allow(self) -> bool:
        """Return True if a Redis call should be attempted right now."""
        with self._lock:
            if self._state == _CB_CLOSED:
                return True
            if self._state == _CB_OPEN:
                if (time.monotonic() - self._opened_at) >= self._cooldown:
                    # Cooldown elapsed: promote to half-open and let ONE probe through.
                    self._state = _CB_HALF_OPEN
                    self._half_open_pending = True
                    self._publish()
                    return True
                return False
            # HALF_OPEN: only the single in-flight probe is allowed.
            if self._half_open_pending:
                return False
            self._half_open_pending = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._half_open_pending = False
            if self._state != _CB_CLOSED:
                self._state = _CB_CLOSED
                self._publish()

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._half_open_pending = False
            # A failed half-open probe, or too many consecutive failures, opens it.
            if self._state == _CB_HALF_OPEN or self._failures >= self._threshold:
                self._state = _CB_OPEN
                self._opened_at = time.monotonic()
                self._publish()

    def reset(self) -> None:
        with self._lock:
            self._state = _CB_CLOSED
            self._failures = 0
            self._half_open_pending = False
            self._publish()


@dataclass
class SegmentResult:
    """Outcome of a /segment lookup."""

    data: bytes
    cache_hit: bool
    origin_fetch_seconds: float  # 0.0 on a hit


class _InMemoryStore:
    """Minimal Redis stand-in (get/set with TTL, flushdb, ping). Fallback only."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[bytes, Optional[float]]] = {}

    def get(self, key: str) -> Optional[bytes]:
        item = self._store.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at is not None and time.monotonic() > expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: bytes, ex: Optional[int] = None) -> None:
        self._store[key] = (value, time.monotonic() + ex if ex else None)

    def flushdb(self) -> None:
        self._store.clear()

    def ping(self) -> bool:
        return True


def _generate_segment_bytes(segment_id: str) -> bytes:
    """Deterministically generate mock segment bytes for a given id."""
    seed = segment_id.encode("utf-8") or b"x"
    reps = (SEGMENT_SIZE_BYTES // len(seed)) + 1
    return (seed * reps)[:SEGMENT_SIZE_BYTES]


class Cache:
    """Edge cache backed by Redis, with an optional in-memory fallback mode."""

    def __init__(self, redis_url: Optional[str] = None, allow_fallback: Optional[bool] = None) -> None:
        self.redis_url = redis_url or os.getenv("REDIS_URL", "redis://localhost:6379/0")
        self.allow_fallback = _fallback_allowed() if allow_fallback is None else allow_fallback

        self._memory: Optional[_InMemoryStore] = None
        self._client = None  # real redis client, or None
        # Last Redis health observed by real request traffic. /healthz reports this
        # WITHOUT doing a live ping, so probes never block on a downstream dependency.
        self._last_redis_ok = False
        # Circuit breaker guarding Redis. Consulted only when a real client exists
        # (strict mode); in memory-fallback mode it stays closed and unused.
        self._cb_enabled = _cb_enabled()
        self._breaker = _CircuitBreaker()

        if _redis is None:
            # redis library not installed at all → memory only.
            self._memory = _InMemoryStore()
            self.backend_name = "memory"
            metrics.redis_up.set(0)
            return

        # Create a lazily-connecting client. from_url does NOT connect immediately;
        # the first command connects and the pool auto-reconnects afterwards.
        # Short timeouts (250ms): during a Redis outage each request should fail fast
        # to origin rather than block a worker thread for a full second.
        redis_timeout = float(os.getenv("REDIS_TIMEOUT_SECONDS", "0.25"))
        self._client = _redis.from_url(
            self.redis_url,
            socket_connect_timeout=redis_timeout,
            socket_timeout=redis_timeout,
        )
        self.backend_name = "redis"

        if self.allow_fallback:
            # Tests / local: probe once. If Redis isn't there, drop to memory so the
            # suite is fast and hermetic (no 1s timeout on every call).
            if not self._ping():
                self._client = None
                self._memory = _InMemoryStore()
                self.backend_name = "memory"
                metrics.redis_up.set(0)
            else:
                self._last_redis_ok = True
                metrics.redis_up.set(1)
        else:
            # Strict mode: keep the lazy client. Start optimistic; real request
            # traffic (_read/_write) keeps redis health accurate from here on.
            self._last_redis_ok = True
            metrics.redis_up.set(1)

    # --- internals ---

    def _ping(self) -> bool:
        if self._client is None:
            return False
        try:
            return bool(self._client.ping())
        except Exception:
            return False

    def _key(self, segment_id: str) -> str:
        return f"segment:{segment_id}"

    def _read(self, key: str) -> Optional[bytes]:
        """Read from the active backend, recording Redis health. Raises nothing."""
        if self._client is not None:
            if self._cb_enabled and not self._breaker.allow():
                # Breaker OPEN: we already know Redis is down. Skip the call so this
                # worker thread doesn't stall on a dead dependency — fail fast to
                # origin. Keep the outage VISIBLE (redis_up stays 0); we drop the
                # stall, not the signal. No new call, so redis_errors_total is not
                # incremented here — the honest reading is "not calling Redis".
                self._last_redis_ok = False
                metrics.redis_up.set(0)
                return None
            try:
                val = self._client.get(key)
                self._breaker.record_success()
                self._last_redis_ok = True
                metrics.redis_up.set(1)
                return val
            except Exception:
                # Strict-mode Redis outage: surface it, do NOT fall back silently.
                self._breaker.record_failure()
                self._last_redis_ok = False
                metrics.redis_up.set(0)
                metrics.redis_errors_total.inc()
                return None
        if self._memory is not None:
            return self._memory.get(key)
        return None

    def _write(self, key: str, value: bytes, ttl: int) -> None:
        if self._client is not None:
            if self._cb_enabled and not self._breaker.allow():
                # Breaker OPEN: don't write to a dead Redis. The freshly fetched
                # segment simply isn't cached this round; it will be on recovery.
                self._last_redis_ok = False
                metrics.redis_up.set(0)
                return
            try:
                self._client.set(key, value, ex=ttl)
                self._breaker.record_success()
                self._last_redis_ok = True
                metrics.redis_up.set(1)
            except Exception:
                self._breaker.record_failure()
                self._last_redis_ok = False
                metrics.redis_up.set(0)
                metrics.redis_errors_total.inc()
            return
        if self._memory is not None:
            self._memory.set(key, value, ex=ttl)

    # --- public API ---

    def get_segment(self, segment_id: str, *, bypass: bool = False) -> SegmentResult:
        """Return a segment, recording whether it was a cache hit or miss.

        bypass=True simulates the cache being unavailable (chaos): always go to
        origin and skip both the read and the write.
        """
        key = self._key(segment_id)

        if not bypass:
            cached = self._read(key)
            if cached is not None:
                return SegmentResult(data=cached, cache_hit=True, origin_fetch_seconds=0.0)

        # Miss (or bypass): fetch from the simulated origin.
        start = time.perf_counter()
        time.sleep(ORIGIN_LATENCY_SECONDS)
        data = _generate_segment_bytes(segment_id)
        elapsed = time.perf_counter() - start

        if not bypass:
            self._write(key, data, SEGMENT_TTL_SECONDS)

        return SegmentResult(data=data, cache_hit=False, origin_fetch_seconds=elapsed)

    def flush(self) -> None:
        """Clear the cache — used by chaos to force a cold cache."""
        try:
            if self._client is not None:
                self._client.flushdb()
            elif self._memory is not None:
                self._memory.flushdb()
        except Exception:
            pass

    def status(self) -> dict:
        """Non-blocking backend status for /healthz.

        Intentionally does NOT ping Redis. Liveness/readiness must reflect the app's
        OWN health, not a shared downstream dependency — otherwise a Redis outage
        marks every replica unhealthy and turns graceful degradation into a hard,
        correlated outage (learned the hard way: see the Phase 4 post-mortem). Redis
        health here is the last value observed by real request traffic, which keeps
        redis_up accurate without ever blocking the probe path.
        """
        if self._client is None:
            return {"backend": "memory", "redis_up": False}
        return {"backend": "redis", "redis_up": self._last_redis_ok}

    def healthy(self) -> bool:
        # The app is "healthy" as long as its process is serving — a Redis outage is a
        # degraded state, not an unhealthy one. So liveness/readiness stay green.
        return True
