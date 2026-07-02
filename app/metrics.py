"""Prometheus metric definitions for the edge-cache service.

These are the signals Prometheus scrapes from /metrics and Grafana later turns into
the four golden signals + cache hit ratio. Defined in one place so the app and tests
import the same objects.

A custom CollectorRegistry is used so tests can import this module repeatedly without
hitting the "duplicated timeseries" error from the global default registry.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Histogram,
    Gauge,
)

# Dedicated registry — exposed on /metrics and used by tests.
registry = CollectorRegistry()

# --- Traffic + errors (golden signals: traffic, errors) ---
http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests handled, labelled by route and status code.",
    ["route", "status"],
    registry=registry,
)

# --- Latency (golden signal: latency) ---
request_latency_seconds = Histogram(
    "request_latency_seconds",
    "End-to-end request latency in seconds, by route.",
    ["route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2),
    registry=registry,
)

# --- Cache behaviour (domain signal: cache hit ratio) ---
cache_hits_total = Counter(
    "cache_hits_total",
    "Number of segment requests served from cache.",
    registry=registry,
)
cache_misses_total = Counter(
    "cache_misses_total",
    "Number of segment requests that missed and went to origin.",
    registry=registry,
)
origin_fetch_latency_seconds = Histogram(
    "origin_fetch_latency_seconds",
    "Latency of simulated origin fetches, in seconds.",
    buckets=(0.01, 0.05, 0.1, 0.2, 0.4, 0.8, 1.6, 3.2),
    registry=registry,
)

# --- Saturation hint: in-flight requests ---
inflight_requests = Gauge(
    "inflight_requests",
    "Number of requests currently being processed.",
    registry=registry,
)

# --- Redis dependency health ---
redis_up = Gauge(
    "redis_up",
    "1 if the last Redis operation succeeded, 0 if Redis was unreachable.",
    registry=registry,
)
redis_errors_total = Counter(
    "redis_errors_total",
    "Number of Redis operations that failed (e.g. during an outage).",
    registry=registry,
)
# Circuit-breaker state guarding the Redis dependency. When Redis fails
# repeatedly the breaker OPENS and requests skip Redis entirely (straight to
# origin) instead of stalling worker threads on a dead dependency. Surfaced as a
# gauge so the dashboard shows exactly when the app stopped calling Redis.
#   0 = closed (normal)   1 = half-open (probing)   2 = open (skipping Redis)
redis_circuit_state = Gauge(
    "redis_circuit_state",
    "Redis circuit-breaker state: 0=closed, 1=half-open, 2=open.",
    registry=registry,
)

# --- Chaos visibility: surface the current injected fault state as gauges so you
# can see on the dashboard exactly when the lab was perturbed. ---
chaos_injected_latency_seconds = Gauge(
    "chaos_injected_latency_seconds",
    "Currently injected artificial latency, in seconds (0 = off).",
    registry=registry,
)
chaos_error_rate = Gauge(
    "chaos_error_rate",
    "Currently injected error rate, 0.0-1.0 (0 = off).",
    registry=registry,
)
chaos_cache_bypass = Gauge(
    "chaos_cache_bypass",
    "1 if the cache is being bypassed (forced origin fetches), else 0.",
    registry=registry,
)
