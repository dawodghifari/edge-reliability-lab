#!/usr/bin/env python3
"""Capacity planning + time-to-saturation forecast for the edge-cache service.

Two questions this answers:
  1. How much traffic can the service take before it breaches its latency SLO —
     both in NORMAL operation and in DEGRADED (Redis-outage / miss-storm) mode?
  2. Given a traffic growth trend, WHEN do we need to add replicas?

Method (documented model — swap the MEASURED INPUTS for real load-step numbers):

  * Concurrency bound (Little's Law): a sync `/segment` request holds one worker
    thread for its whole service time W. With N worker threads per replica, the
    max sustainable throughput is  N / W  requests/sec/replica.
      - Normal ops: W ≈ warm-cache latency (a few ms) → thread bound is huge, so
        CPU binds first.
      - Degraded ops (Redis down): every request is an origin fetch, W ≈ 150 ms,
        so a replica can only sustain N / 0.15 ≈ 267 rps before threads saturate
        and latency runs away. The circuit breaker is what makes this a *bounded*
        number: it keeps W pinned at the origin cost instead of letting it grow
        unbounded on a dead Redis (the worker-thread stall from the incident).
  * CPU bound (normal ops): a flat rps/replica ceiling. This is the value most
    worth replacing with a real measurement (see CAPACITY.md → "collecting data").

  * Forecast: exponential traffic growth  peak(t) = peak0 * (1+g)^t . Time to reach
    a capacity threshold C is  t = ln(C / peak0) / ln(1+g).  (Same approach as the
    NetOps project's saturation forecast.)

Run:  python3 capacity_forecast.py    → prints a summary and writes capacity-forecast.png
"""

from __future__ import annotations

import math
from pathlib import Path

import matplotlib

matplotlib.use("Agg")  # headless: write a PNG, no display needed
import matplotlib.pyplot as plt
import numpy as np

# ---------------------------------------------------------------------------
# MEASURED INPUTS  — replace these with real numbers from a load-step run.
# (See CAPACITY.md for the exact k6 stages + PromQL to collect them.)
# ---------------------------------------------------------------------------
WORKERS_PER_REPLICA = 40      # anyio/uvicorn default thread pool for sync endpoints
REPLICAS = 2                  # k8s/edge-cache.yaml: spec.replicas
W_HIT_S = 0.003               # warm-cache p50 (~few ms) — dashboard latency panel
W_MISS_S = 0.150              # origin fetch latency — ORIGIN_LATENCY_SECONDS
SLO_P99_S = 0.200             # latency SLO: 99% of /segment < 200 ms

# CPU-bound normal-ops ceiling per replica (rps). MODELLED placeholder — this is
# the headline number to replace with a real load-step measurement.
CPU_BOUND_RPS_PER_REPLICA = 1500

# Traffic trend for the forecast (organic peak, not the k6 synthetic peak).
CURRENT_PEAK_RPS = 800.0      # today's busy-hour peak
MONTHLY_GROWTH = 0.12         # +12% / month
ADD_REPLICA_AT = 0.70         # provision headroom: act at 70% of normal capacity


def per_replica_normal() -> float:
    """Normal-ops capacity per replica: min(thread bound, CPU bound)."""
    thread_bound = WORKERS_PER_REPLICA / W_HIT_S
    return min(thread_bound, CPU_BOUND_RPS_PER_REPLICA)


def per_replica_degraded() -> float:
    """Degraded (Redis-down) capacity per replica: threads held by origin fetches."""
    thread_bound = WORKERS_PER_REPLICA / W_MISS_S
    return min(thread_bound, CPU_BOUND_RPS_PER_REPLICA)


def months_to(threshold: float, peak0: float = CURRENT_PEAK_RPS, g: float = MONTHLY_GROWTH) -> float:
    """Months until exponential-growth traffic reaches `threshold` rps."""
    if peak0 <= 0 or threshold <= peak0:
        return 0.0
    return math.log(threshold / peak0) / math.log(1.0 + g)


def replicas_needed(target_rps: float, per_replica: float) -> int:
    return max(1, math.ceil(target_rps / per_replica))


def summarise() -> dict:
    norm_pr = per_replica_normal()
    degr_pr = per_replica_degraded()
    norm_total = norm_pr * REPLICAS
    degr_total = degr_pr * REPLICAS
    add_at = ADD_REPLICA_AT * norm_total

    print("=" * 68)
    print("  edge-cache capacity forecast")
    print("=" * 68)
    print(f"  Workers/replica ........... {WORKERS_PER_REPLICA}")
    print(f"  Replicas .................. {REPLICAS}")
    print(f"  W (hit / miss) ............ {W_HIT_S*1000:.0f} ms / {W_MISS_S*1000:.0f} ms")
    print("-" * 68)
    print(f"  Per-replica capacity (normal) .... {norm_pr:,.0f} rps  (CPU-bound)")
    print(f"  Per-replica capacity (degraded) .. {degr_pr:,.0f} rps  (thread-bound, miss-storm)")
    print(f"  TOTAL capacity (normal) .......... {norm_total:,.0f} rps")
    print(f"  TOTAL capacity (degraded) ........ {degr_total:,.0f} rps")
    print("-" * 68)
    print(f"  Current peak .............. {CURRENT_PEAK_RPS:,.0f} rps  ({MONTHLY_GROWTH*100:.0f}%/mo growth)")
    print(f"  Normal utilisation ........ {CURRENT_PEAK_RPS / norm_total * 100:.0f}%")
    print(f"  Add a replica (@{ADD_REPLICA_AT*100:.0f}%) in ... {months_to(add_at):.1f} months")
    print(f"  Reach normal capacity in .. {months_to(norm_total):.1f} months")
    print("-" * 68)
    print("  Outage-survivability (the incident insight):")
    print(f"    To survive a Redis outage AT TODAY'S PEAK ({CURRENT_PEAK_RPS:,.0f} rps) without")
    print(f"    breaching latency, you need {replicas_needed(CURRENT_PEAK_RPS, degr_pr)} replicas "
          f"(have {REPLICAS}).")
    print(f"    Degraded headroom today = {degr_total / CURRENT_PEAK_RPS:.1f}x peak.")
    print("=" * 68)

    return {
        "norm_pr": norm_pr, "degr_pr": degr_pr,
        "norm_total": norm_total, "degr_total": degr_total, "add_at": add_at,
    }


def make_chart(s: dict, out: Path) -> None:
    months = np.arange(0, 19)
    traffic = CURRENT_PEAK_RPS * (1.0 + MONTHLY_GROWTH) ** months

    fig, ax = plt.subplots(figsize=(10, 6))
    ax.plot(months, traffic, color="#2563eb", lw=2.5, label=f"Projected peak (+{MONTHLY_GROWTH*100:.0f}%/mo)")

    ax.axhline(s["norm_total"], color="#dc2626", ls="--", lw=1.8,
               label=f"Normal capacity ({s['norm_total']:,.0f} rps, {REPLICAS} replicas)")
    ax.axhline(s["add_at"], color="#f59e0b", ls="--", lw=1.8,
               label=f"Add-replica threshold ({ADD_REPLICA_AT*100:.0f}% = {s['add_at']:,.0f} rps)")
    ax.axhline(s["degr_total"], color="#7c3aed", ls=":", lw=1.8,
               label=f"Degraded/outage capacity ({s['degr_total']:,.0f} rps)")

    # Mark the crossover points.
    for thr, col, lbl in [(s["add_at"], "#f59e0b", "add replica"),
                          (s["norm_total"], "#dc2626", "at capacity")]:
        t = months_to(thr)
        if 0 < t < 18:
            ax.plot([t], [thr], "o", color=col, ms=8, zorder=5)
            ax.annotate(f"{lbl}\n~{t:.1f} mo", (t, thr), textcoords="offset points",
                        xytext=(8, -28), fontsize=9, color=col)

    ax.set_xlabel("Months from now")
    ax.set_ylabel("Requests / sec (busy-hour peak)")
    ax.set_title("edge-cache — traffic growth vs capacity (time-to-saturation)")
    ax.set_xlim(0, 18)
    ax.set_ylim(0, max(s["norm_total"] * 1.15, traffic.max() * 1.05))
    ax.grid(True, alpha=0.25)
    ax.legend(loc="upper left", fontsize=9, framealpha=0.9)
    fig.tight_layout()
    fig.savefig(out, dpi=130)
    print(f"\nChart written to {out}")


if __name__ == "__main__":
    stats = summarise()
    make_chart(stats, Path(__file__).with_name("capacity-forecast.png"))
