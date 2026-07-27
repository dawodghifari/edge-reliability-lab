"""Distributed tracing for the edge-cache service (OpenTelemetry -> Tempo).

Metrics tell you *that* the service is slow. Traces tell you *where the time went*
inside one request. This module wires up the third observability pillar so a single
`/segment` request breaks down into a waterfall:

    GET /segment/abc123                    812ms
    |-- cache.lookup   (redis GET)          11ms   cache.hit=false
    `-- origin.fetch                       798ms

Spans are exported over OTLP/HTTP to a collector (Grafana Tempo in the cluster).

Design decisions worth knowing:

- **Disabled unless configured.** If OTEL_EXPORTER_OTLP_ENDPOINT is unset, tracing
  is a no-op. Tests, docker-compose without Tempo, and anyone cloning the repo all
  keep working with zero setup. Observability must never be a hard dependency of
  the thing it observes.

- **Sampled, not firehosed.** The incident drill runs ~128 rps. Exporting every
  span at that rate adds real overhead to the request path and would perturb the
  very latency numbers the drill exists to measure. TRACE_SAMPLE_RATIO defaults to
  0.1 (10%) so the p99 stays trustworthy; set it to 1.0 locally when you want to
  see every request.

- **/healthz and /metrics are excluded.** Kubernetes probes fire every few seconds
  and Prometheus scrapes every 15s across every replica. Tracing them would swamp
  the backend with spans that never diagnose anything.

- **Batched export.** BatchSpanProcessor buffers spans on a background thread, so a
  slow or dead collector can't block a request. A tracing backend going down must
  degrade the telemetry, never the service.

Environment:
    OTEL_EXPORTER_OTLP_ENDPOINT   e.g. http://tempo.monitoring:4318  (unset = off)
    OTEL_SERVICE_NAME             defaults to "edge-cache"
    TRACE_SAMPLE_RATIO            0.0-1.0, default 0.1
    DEPLOYMENT_ENV                free text, default "lab"
"""

from __future__ import annotations

import logging
import os
from typing import Optional

log = logging.getLogger(__name__)

# Populated by setup_tracing(); None means tracing is off.
_tracer = None
_enabled = False


def tracing_enabled() -> bool:
    """True once setup_tracing() has successfully configured an exporter."""
    return _enabled


def get_tracer():
    """Return the tracer, or None when tracing is disabled.

    Callers should use start_span() below rather than touching this directly.
    """
    return _tracer


def _sample_ratio() -> float:
    raw = os.getenv("TRACE_SAMPLE_RATIO", "0.1")
    try:
        return min(1.0, max(0.0, float(raw)))
    except ValueError:
        log.warning("Invalid TRACE_SAMPLE_RATIO=%r, falling back to 0.1", raw)
        return 0.1


def setup_tracing(app=None) -> bool:
    """Configure OpenTelemetry. Returns True if tracing was enabled.

    Safe to call when the OTel packages aren't installed or no endpoint is set —
    it logs and returns False rather than raising. The service must start either way.
    """
    global _tracer, _enabled

    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    if not endpoint:
        log.info("Tracing disabled (OTEL_EXPORTER_OTLP_ENDPOINT not set).")
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
    except ImportError as exc:  # pragma: no cover - only when extras aren't installed
        log.warning("Tracing requested but OpenTelemetry is not installed (%s).", exc)
        return False

    ratio = _sample_ratio()
    resource = Resource.create(
        {
            "service.name": os.getenv("OTEL_SERVICE_NAME", "edge-cache"),
            "service.version": os.getenv("SERVICE_VERSION", "0.1.0"),
            "deployment.environment": os.getenv("DEPLOYMENT_ENV", "lab"),
        }
    )

    # ParentBased: if an upstream service already decided to sample this trace, honour
    # that decision so a trace is never half-recorded. Only *new* roots get the dice roll.
    provider = TracerProvider(
        resource=resource,
        sampler=ParentBased(root=TraceIdRatioBased(ratio)),
    )
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint.rstrip('/')}/v1/traces"))
    )
    trace.set_tracer_provider(provider)

    _tracer = trace.get_tracer("edge-cache")
    _enabled = True

    _instrument_libraries(app)
    log.info("Tracing enabled -> %s (sample ratio %.2f)", endpoint, ratio)
    return True


def _instrument_libraries(app) -> None:
    """Turn on automatic instrumentation for FastAPI and redis-py."""
    if app is not None:
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            # Probes and scrapes are high-frequency and diagnostically worthless as
            # traces — keep them out so the backend holds real request traffic.
            FastAPIInstrumentor.instrument_app(app, excluded_urls="healthz,metrics")
        except Exception as exc:  # pragma: no cover
            log.warning("FastAPI instrumentation failed: %s", exc)

    try:
        from opentelemetry.instrumentation.redis import RedisInstrumentor

        RedisInstrumentor().instrument()
    except Exception as exc:  # pragma: no cover
        log.warning("Redis instrumentation failed: %s", exc)


class _NullSpan:
    """Stand-in span used when tracing is off, so call sites need no if-checks."""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_attribute(self, *_args, **_kwargs):
        pass

    def add_event(self, *_args, **_kwargs):
        pass


def start_span(name: str, **attributes):
    """Context manager for a child span; a no-op when tracing is disabled.

        with start_span("origin.fetch", **{"segment.id": sid}) as span:
            ...
            span.set_attribute("cache.hit", False)
    """
    if _tracer is None:
        return _NullSpan()
    return _tracer.start_as_current_span(name, attributes=attributes or None)


def current_trace_id() -> Optional[str]:
    """Hex trace id of the active span — but ONLY if that trace is being sampled.

    Used to attach Prometheus exemplars, which is what lets you click a latency
    spike in Grafana and land on the exact slow request behind it.

    The sampled check is the whole point of this function, and it is easy to miss:
    an UNSAMPLED span still has a perfectly valid trace id. It just never gets
    exported. Attaching that id as an exemplar produces a link that looks fine in
    Grafana and dead-ends in "failed to get trace with id ... Not Found", because
    Tempo never received it. At a 10% sample ratio that is ~90% of exemplars.

    So: only emit an exemplar when the trace behind it will actually exist. Fewer
    diamonds on the panel, but every one of them resolves. A broken link is worse
    than no link — it teaches people not to trust the tooling.
    """
    if _tracer is None:
        return None
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid and ctx.trace_id and ctx.trace_flags.sampled:
            return format(ctx.trace_id, "032x")
    except Exception:  # pragma: no cover
        pass
    return None
