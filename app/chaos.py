"""Runtime fault injection — the lever we'll pull to cause a controlled incident.

This is deliberately a *lab feature*: a single in-process state object plus a couple
of helpers. Toggling it via POST /admin/chaos lets us inject artificial latency, a
random error rate, and a cache bypass at runtime, then watch SLOs burn and alerts
fire — without redeploying.

SAFETY GUARD: the /admin/chaos endpoint is only mounted when CHAOS_ENABLED=true (the
default in this lab). In any environment where you don't want it, set
CHAOS_ENABLED=false and the endpoint returns 404.
"""

from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, asdict

from . import metrics


def chaos_enabled() -> bool:
    return os.getenv("CHAOS_ENABLED", "true").lower() in ("1", "true", "yes")


@dataclass
class ChaosState:
    """Current injected-fault configuration."""

    latency_seconds: float = 0.0  # extra latency added to every request
    error_rate: float = 0.0       # probability (0-1) a request is failed with 500
    cache_bypass: bool = False    # force every read to miss → origin

    def clamp(self) -> None:
        self.latency_seconds = max(0.0, float(self.latency_seconds))
        self.error_rate = min(1.0, max(0.0, float(self.error_rate)))
        self.cache_bypass = bool(self.cache_bypass)

    def publish_metrics(self) -> None:
        """Reflect current state into Prometheus gauges for dashboard visibility."""
        metrics.chaos_injected_latency_seconds.set(self.latency_seconds)
        metrics.chaos_error_rate.set(self.error_rate)
        metrics.chaos_cache_bypass.set(1 if self.cache_bypass else 0)


# Single shared state for the process.
state = ChaosState()
state.publish_metrics()


def apply_latency() -> None:
    """Sleep for the currently injected latency, if any."""
    if state.latency_seconds > 0:
        time.sleep(state.latency_seconds)


def should_error() -> bool:
    """Return True if this request should be failed, per the injected error rate."""
    if state.error_rate <= 0:
        return False
    return random.random() < state.error_rate


def update(**kwargs) -> dict:
    """Update chaos state from a partial dict and return the new state as a dict."""
    if "latency_seconds" in kwargs and kwargs["latency_seconds"] is not None:
        state.latency_seconds = float(kwargs["latency_seconds"])
    if "error_rate" in kwargs and kwargs["error_rate"] is not None:
        state.error_rate = float(kwargs["error_rate"])
    if "cache_bypass" in kwargs and kwargs["cache_bypass"] is not None:
        state.cache_bypass = bool(kwargs["cache_bypass"])
    state.clamp()
    state.publish_metrics()
    return asdict(state)


def reset() -> dict:
    """Turn all chaos off."""
    state.latency_seconds = 0.0
    state.error_rate = 0.0
    state.cache_bypass = False
    state.publish_metrics()
    return asdict(state)
