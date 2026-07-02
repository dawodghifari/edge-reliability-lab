"""Phase 0 tests for the edge-cache service.

Run from the repo root:  pytest -q

These tests don't need a live Redis — the Cache layer falls back to an in-memory
backend when Redis is unreachable, so the hit/miss logic is fully exercised here.
We keep origin latency tiny to keep the suite fast.
"""

import os

# Make the simulated origin fast for tests, before importing the app.
os.environ.setdefault("ORIGIN_LATENCY_SECONDS", "0.01")
os.environ.setdefault("CHAOS_ENABLED", "true")
# No Redis server in the test environment → allow the in-memory fallback so the
# hit/miss logic is exercised without a live Redis (and without 1s timeouts).
os.environ.setdefault("CACHE_MEMORY_FALLBACK", "true")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, cache  # noqa: E402
from app import chaos  # noqa: E402

client = TestClient(app)


def setup_function(_):
    """Start each test from a clean cache and no chaos."""
    cache.flush()
    chaos.reset()


def test_healthz():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_miss_then_hit():
    """First request to a fresh id is a MISS; the second is a HIT."""
    r1 = client.get("/segment/abc")
    assert r1.status_code == 200
    assert r1.headers["X-Cache"] == "MISS"

    r2 = client.get("/segment/abc")
    assert r2.status_code == 200
    assert r2.headers["X-Cache"] == "HIT"

    # Same id → identical bytes whether from origin or cache.
    assert r1.content == r2.content


def test_metrics_endpoint_and_counters():
    """/metrics exposes Prometheus text and counters move with traffic."""
    # Warm one miss + one hit.
    client.get("/segment/xyz")
    client.get("/segment/xyz")

    r = client.get("/metrics")
    assert r.status_code == 200
    body = r.text
    assert "cache_hits_total" in body
    assert "cache_misses_total" in body
    assert "http_requests_total" in body
    assert "request_latency_seconds" in body


def test_chaos_error_injection():
    """Injecting a 100% error rate makes /segment return 500."""
    resp = client.post("/admin/chaos", json={"error_rate": 1.0})
    assert resp.status_code == 200
    assert resp.json()["state"]["error_rate"] == 1.0

    r = client.get("/segment/will-fail")
    assert r.status_code == 500

    # Reset restores healthy behaviour.
    client.post("/admin/chaos", json={"reset": True})
    assert client.get("/segment/will-fail").status_code == 200


def test_chaos_cache_bypass_forces_miss():
    """With cache_bypass on, even a warmed id keeps reporting MISS."""
    client.get("/segment/warm")          # warms the cache
    assert client.get("/segment/warm").headers["X-Cache"] == "HIT"

    client.post("/admin/chaos", json={"cache_bypass": True})
    assert client.get("/segment/warm").headers["X-Cache"] == "MISS"


def test_flush_cache_forces_cold():
    """flush_cache clears warmed entries → next read is a MISS again."""
    client.get("/segment/cold")
    assert client.get("/segment/cold").headers["X-Cache"] == "HIT"

    client.post("/admin/chaos", json={"flush_cache": True})
    assert client.get("/segment/cold").headers["X-Cache"] == "MISS"
