"""FastAPI edge-cache service.

A small honest mirror of an edge video-delivery node: clients request video
"segments", which we serve from a Redis cache, falling back to a simulated origin
on a miss. Everything is instrumented for Prometheus so we can later define SLOs,
alert on them, and break the service on purpose.

Routes:
  GET  /segment/{id}   serve a mock segment (cache hit/miss + origin sim)
  GET  /healthz        liveness/readiness probe
  GET  /metrics        Prometheus exposition
  POST /admin/chaos    toggle injected faults (lab-only; guarded by CHAOS_ENABLED)
  GET  /admin/chaos    read current chaos state
"""

from __future__ import annotations

import time
from typing import Optional

from fastapi import FastAPI, Response, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from prometheus_client.openmetrics.exposition import (
    generate_latest as generate_latest_openmetrics,
    CONTENT_TYPE_LATEST as OPENMETRICS_CONTENT_TYPE,
)

from . import metrics
from . import chaos
from . import tracing
from .cache import Cache

try:  # optional: absent when the OTel extras aren't installed
    from opentelemetry import trace as trace_api
except ImportError:  # pragma: no cover
    trace_api = None

app = FastAPI(
    title="Edge Reliability Lab — edge-cache",
    description="Mock edge video-cache service instrumented the SRE way.",
    version="0.1.0",
)

# Tracing is opt-in: a no-op unless OTEL_EXPORTER_OTLP_ENDPOINT is set. Must run
# before the first request so FastAPI auto-instrumentation can attach middleware.
tracing.setup_tracing(app)

# One cache instance per process. Reads REDIS_URL from the environment.
cache = Cache()


def _observe(route: str, status: int, started: float) -> None:
    """Record latency + request count for a finished request.

    Where a trace is in flight, its id rides along as a Prometheus *exemplar* — the
    link that lets you click a latency spike in Grafana and land on the exact slow
    request behind it. Exemplars only survive OpenMetrics exposition, so /metrics
    negotiates that content type below.
    """
    elapsed = time.perf_counter() - started
    trace_id = tracing.current_trace_id()
    latency = metrics.request_latency_seconds.labels(route=route)
    if trace_id:
        try:
            latency.observe(elapsed, exemplar={"trace_id": trace_id})
        except (TypeError, ValueError):
            # Older prometheus_client, or a label set that rejects exemplars —
            # record the measurement anyway. Losing a trace link must never lose data.
            latency.observe(elapsed)
    else:
        latency.observe(elapsed)
    metrics.http_requests_total.labels(route=route, status=str(status)).inc()


@app.get("/healthz")
async def healthz() -> JSONResponse:
    """Liveness/readiness. ASYNC on purpose: it runs on the event loop, not the
    request thread pool, so it responds instantly even when every worker thread is
    blocked on a slow Redis/origin during an outage. A health check must never be
    starved by the very failure it's meant to report. status() is non-blocking."""
    started = time.perf_counter()
    status = cache.status()
    body = {"status": "ok", "cache_backend": status["backend"], "redis_up": status["redis_up"]}
    _observe("/healthz", 200, started)
    return JSONResponse(body)


@app.get("/metrics")
async def get_metrics(request: Request) -> Response:
    """Prometheus exposition endpoint. ASYNC so scrapes aren't starved under load.

    Serves OpenMetrics when the scraper asks for it (Prometheus sends
    `Accept: application/openmetrics-text` when exemplar storage is enabled).
    Exemplars — our metric-to-trace links — exist ONLY in that format; the classic
    text format silently drops them, which looks exactly like tracing being broken.
    """
    accept = request.headers.get("accept", "")
    if "application/openmetrics-text" in accept:
        return Response(
            generate_latest_openmetrics(metrics.registry),
            media_type=OPENMETRICS_CONTENT_TYPE,
        )
    return Response(generate_latest(metrics.registry), media_type=CONTENT_TYPE_LATEST)


@app.get("/segment/{segment_id}")
def get_segment(segment_id: str) -> Response:
    """Serve a mock video segment through the cache."""
    started = time.perf_counter()
    metrics.inflight_requests.inc()
    try:
        # Chaos: injected latency applies to every request.
        chaos.apply_latency()

        # Chaos: injected error rate fails a fraction of requests with a 500.
        if chaos.should_error():
            _observe("/segment", 500, started)
            return JSONResponse({"error": "injected fault"}, status_code=500)

        result = cache.get_segment(segment_id, bypass=chaos.state.cache_bypass)

        # Tag the request span with the outcome so you can filter Tempo for the
        # interesting traces — e.g. every miss during the outage window.
        span = trace_api.get_current_span() if trace_api else None
        if span is not None:
            span.set_attribute("segment.id", segment_id)
            span.set_attribute("cache.hit", result.cache_hit)
            span.set_attribute("cache.bypass", bool(chaos.state.cache_bypass))
            span.set_attribute("redis.circuit_state", cache.breaker_state())

        if result.cache_hit:
            metrics.cache_hits_total.inc()
        else:
            metrics.cache_misses_total.inc()
            metrics.origin_fetch_latency_seconds.observe(result.origin_fetch_seconds)

        _observe("/segment", 200, started)
        return Response(
            content=result.data,
            media_type="application/octet-stream",
            headers={
                "X-Cache": "HIT" if result.cache_hit else "MISS",
                "X-Segment-Id": segment_id,
            },
        )
    finally:
        metrics.inflight_requests.dec()


# --- Chaos admin (lab-only) ---

class ChaosUpdate(BaseModel):
    latency_seconds: Optional[float] = None
    error_rate: Optional[float] = None
    cache_bypass: Optional[bool] = None
    flush_cache: Optional[bool] = None  # one-shot: clear the cache to force a cold start
    reset: Optional[bool] = None        # turn all chaos off


def _require_chaos() -> None:
    if not chaos.chaos_enabled():
        raise HTTPException(status_code=404, detail="Not Found")


@app.get("/admin/chaos")
def read_chaos() -> JSONResponse:
    _require_chaos()
    from dataclasses import asdict
    return JSONResponse(asdict(chaos.state))


@app.post("/admin/chaos")
def set_chaos(update: ChaosUpdate) -> JSONResponse:
    """Toggle injected faults at runtime. LAB FEATURE — guarded by CHAOS_ENABLED."""
    _require_chaos()

    actions = []
    if update.reset:
        new_state = chaos.reset()
        actions.append("reset")
    else:
        new_state = chaos.update(
            latency_seconds=update.latency_seconds,
            error_rate=update.error_rate,
            cache_bypass=update.cache_bypass,
        )
        if update.flush_cache:
            cache.flush()
            actions.append("flushed_cache")

    return JSONResponse({"state": new_state, "actions": actions})
