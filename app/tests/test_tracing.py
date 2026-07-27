"""Tests for the tracing layer.

Two things matter here and both are easy to get wrong silently:

1. With no OTLP endpoint configured the service must behave *exactly* as before.
   Observability is not allowed to be a hard dependency of the thing it observes.
2. With tracing on, the spans must actually describe the cache decision — the
   whole point is that a waterfall explains why a request was slow.

Spans are captured with an in-memory exporter, so these tests need no collector.
"""

from __future__ import annotations

import importlib

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_module(monkeypatch):
    """Import the app fresh with memory-cache fallback and tracing off."""
    monkeypatch.setenv("CACHE_MEMORY_FALLBACK", "true")
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    import app.tracing as tracing_mod
    import app.main as main_mod

    importlib.reload(tracing_mod)
    importlib.reload(main_mod)
    return main_mod


def test_service_works_with_tracing_disabled(app_module):
    """No endpoint configured → tracing off, service fully functional."""
    from app import tracing

    assert tracing.tracing_enabled() is False

    client = TestClient(app_module.app)
    first = client.get("/segment/abc")
    second = client.get("/segment/abc")

    assert first.status_code == 200
    assert first.headers["X-Cache"] == "MISS"
    assert second.headers["X-Cache"] == "HIT"


def test_start_span_is_a_noop_when_disabled(app_module):
    """The null span must swallow the same calls a real span accepts, so call
    sites never need to guard on whether tracing is on."""
    from app import tracing

    with tracing.start_span("anything", **{"some.attr": 1}) as span:
        span.set_attribute("cache.hit", True)
        span.add_event("nothing happens")

    assert tracing.current_trace_id() is None


def test_metrics_endpoint_still_serves_plain_text(app_module):
    """Without an OpenMetrics Accept header we must serve the classic format —
    scrapers that don't understand OpenMetrics must keep working."""
    client = TestClient(app_module.app)
    resp = client.get("/metrics")

    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "request_latency_seconds" in resp.text


def test_unsampled_spans_produce_no_exemplar():
    """Regression: an unsampled span has a valid trace id but is never exported.

    Emitting it as a Prometheus exemplar creates a link that renders normally in
    Grafana and dead-ends in "trace not found" — at a 10% sample ratio, for ~90%
    of exemplars. current_trace_id() must return None unless the trace is sampled.
    """
    pytest.importorskip("opentelemetry.sdk.trace")

    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.sampling import ALWAYS_OFF

    import app.tracing as tracing_mod

    importlib.reload(tracing_mod)
    # Own provider, not setup_tracing() — see the note in the sampled-case test.
    provider = sdk.TracerProvider(sampler=ALWAYS_OFF)
    tracing_mod._tracer = provider.get_tracer("test")
    tracing_mod._enabled = True

    with tracing_mod.start_span("unsampled"):
        assert tracing_mod.current_trace_id() is None


def test_sampled_spans_produce_an_exemplar():
    """The other half: when the trace IS sampled, we must emit the id.

    Builds its own TracerProvider rather than calling setup_tracing(), because
    OpenTelemetry permits the *global* provider to be set only once per process —
    a second setup_tracing() in the same test run is silently ignored and would
    leave this test asserting against the previous test's sampler.
    """
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON

    import app.tracing as tracing_mod

    importlib.reload(tracing_mod)
    provider = sdk.TracerProvider(sampler=ALWAYS_ON)
    tracing_mod._tracer = provider.get_tracer("test")
    tracing_mod._enabled = True

    with tracing_mod.start_span("sampled"):
        trace_id = tracing_mod.current_trace_id()

    assert trace_id is not None
    assert len(trace_id) == 32
    int(trace_id, 16)  # must be valid hex


def test_spans_describe_the_cache_decision(monkeypatch):
    """With tracing on, a miss then a hit must produce spans carrying the cache
    outcome and the breaker state."""
    sdk = pytest.importorskip("opentelemetry.sdk.trace")
    from opentelemetry import trace as trace_api
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setenv("CACHE_MEMORY_FALLBACK", "true")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("TRACE_SAMPLE_RATIO", "1.0")

    import app.tracing as tracing_mod
    import app.cache as cache_mod

    importlib.reload(tracing_mod)
    importlib.reload(cache_mod)

    # Swap the batching OTLP exporter for an in-memory one so nothing leaves the
    # process and assertions are synchronous.
    exporter = InMemorySpanExporter()
    provider = sdk.TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace_api.set_tracer_provider(provider)
    tracing_mod._tracer = provider.get_tracer("test")
    tracing_mod._enabled = True

    cache = cache_mod.Cache(allow_fallback=True)
    miss = cache.get_segment("seg-1")
    hit = cache.get_segment("seg-1")

    assert miss.cache_hit is False
    assert hit.cache_hit is True

    spans = {s.name: s for s in exporter.get_finished_spans()}
    assert "cache.lookup" in spans
    assert "origin.fetch" in spans

    origin = spans["origin.fetch"]
    assert origin.attributes["segment.id"] == "seg-1"
    assert origin.attributes["origin.fetch_seconds"] > 0

    lookup = spans["cache.lookup"]
    assert lookup.attributes["redis.circuit_state"] in ("closed", "half_open", "open")
