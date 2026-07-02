# Capacity planning & time-to-saturation

How much traffic can `edge-cache` take before it breaks its latency SLO, and when
will organic growth force us to add replicas? This is a **model** (run
`capacity_forecast.py` to reproduce); the inputs are labelled so real load-step
measurements can replace them — see [collecting real data](#collecting-real-data).

![Traffic growth vs capacity](capacity-forecast.png)

## The model

`/segment` is a **synchronous** endpoint, so each in-flight request holds one worker
thread for its whole service time `W`. Two ceilings decide capacity, and the lower one
wins:

**1. Concurrency bound (Little's Law).** With `N` worker threads per replica, max
sustainable throughput is `N / W` rps/replica.

| Mode | Service time `W` | Thread-bound rps/replica |
|------|------------------|--------------------------|
| Normal (cache hit) | ~3 ms | ~13,300 (effectively unlimited → CPU binds first) |
| **Degraded (Redis down)** | ~150 ms (origin fetch) | **~267** |

**2. CPU bound (normal ops).** A flat rps/replica ceiling from the 500m CPU limit —
modelled here at **1,500 rps/replica** (the number most worth measuring for real).

Effective capacity is `min(thread_bound, cpu_bound)` per replica, times the replica
count.

| | Per replica | Total (2 replicas) | Binding constraint |
|---|---|---|---|
| **Normal** | 1,500 rps | **3,000 rps** | CPU |
| **Degraded (outage)** | 267 rps | **~533 rps** | worker threads (each miss holds one for 150 ms) |

## Growth forecast

Exponential model `peak(t) = peak₀ · (1 + g)^t`; time to a threshold `C` is
`t = ln(C / peak₀) / ln(1 + g)`. With a starting busy-hour peak of **800 rps** growing
**12%/month**:

- **~27%** of normal capacity used today.
- Cross the **70% add-replica threshold (2,100 rps) in ~8.5 months** → provision a 3rd
  replica then.
- Reach **normal capacity (3,000 rps) in ~11.7 months**.

## The capacity insight that comes from the incident

The degraded capacity (~533 rps) is *far* below the normal capacity (3,000 rps), because
during a Redis outage every request becomes a 150 ms origin fetch and ties up a worker
thread. **The circuit breaker is what makes this a finite, plannable number** — it pins
service time at the origin cost instead of letting it grow unbounded on a dead Redis (the
worker-thread stall, which drove effective capacity toward zero).

Consequence for capacity planning: surviving a Redis outage *at peak* is a much stricter
constraint than serving normal traffic.

> At today's 800 rps peak, a Redis outage needs **3 replicas** to stay under the latency
> SLO — but we run **2**. Degraded headroom is only ~0.7× peak.

Three levers close that gap (they map to the post-mortem action items):
1. **More replicas** — brute force. ~3 replicas cover today's 800 rps peak in outage mode;
   the ~2,400 rps we drove in load tests would need ~9.
2. **Lower origin latency** — halving `W_miss` doubles degraded capacity.
3. **A short-TTL micro-cache** (post-mortem action item #2) — keeps a fraction of
   requests fast even with Redis down, raising effective degraded capacity without hiding
   the outage.

## Collecting real data

Replace the `MEASURED INPUTS` block in `capacity_forecast.py` with numbers from a
load-step run:

1. Ramp k6 in stages until p99 crosses the 200 ms SLO (edit `load/k6-loadtest.js` stages,
   e.g. `50 → 100 → 200 → 400 → 800` VUs, 2 min each).
2. Record the throughput at the last stage where p99 stayed < 200 ms — that's the real
   **normal** capacity. In Prometheus:

   ```promql
   sum(rate(http_requests_total{job="edge-cache",route="/segment"}[1m]))
   ```

   alongside the latency SLI:

   ```promql
   1 - (sum(rate(request_latency_seconds_bucket{job="edge-cache",route="/segment",le="0.2"}[1m]))
        / sum(rate(request_latency_seconds_count{job="edge-cache",route="/segment"}[1m])))
   ```

3. For the **degraded** number, repeat the ramp with Redis scaled to 0 (breaker open) and
   find where p99 breaches — that validates the modelled ~267 rps/replica.
4. Set `CURRENT_PEAK_RPS` / `MONTHLY_GROWTH` from your real traffic history and re-run.
