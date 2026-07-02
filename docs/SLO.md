# Service Level Objectives — edge-cache

This service exists to serve video segments quickly and reliably. These SLOs define
what "reliable enough" means, so we can alert on real user pain instead of on every
blip, and spend the resulting **error budget** deliberately.

The user-facing surface we measure is the `GET /segment/{id}` endpoint. Health checks
(`/healthz`) and admin routes are excluded from the SLIs.

## SLIs and SLOs

| # | SLO | SLI (what we measure) | Target | Window |
|---|-----|-----------------------|--------|--------|
| 1 | **Availability** | proportion of `/segment` requests that return a non-5xx status | **99.9%** | 30 days |
| 2 | **Latency** | proportion of `/segment` requests served in **< 200 ms** | **99%** | 30 days |

"Good" for latency is a request that lands in the `le="0.2"` histogram bucket. We use
the *proportion of fast requests* rather than a raw p99 line, because it composes
cleanly into an error budget.

## Error budgets

The error budget is the amount of unreliability we're allowed before the SLO is
breached — i.e. `1 - target`.

| SLO | Target | Error budget | Meaning over 30 days |
|-----|--------|--------------|----------------------|
| Availability | 99.9% | **0.1%** of requests may be 5xx | ~43 min of "all requests failing" equivalent |
| Latency | 99% | **1%** of requests may be ≥ 200 ms | 10x larger budget than availability |

When the budget is healthy, we can ship changes and run chaos experiments freely.
When it's burning fast, the priority shifts to reliability.

## Alerting: multi-window, multi-burn-rate

Rather than "alert if error rate > X", we alert on the **rate at which the error
budget is being consumed** (the "burn rate"), across two windows at once. A single
short window is jumpy; a single long window is slow. Requiring both a long and a
short window to be over the threshold gives fast detection with few false alarms
(Google SRE workbook, ch. 5).

Burn rate `= observed bad-ratio / error-budget`. A burn rate of 1 exactly exhausts
the 30-day budget in 30 days; 14.4 exhausts it in ~2 days.

| Alert | Burn rate | Windows (long AND short) | Severity | Budget consumed before firing |
|-------|-----------|--------------------------|----------|-------------------------------|
| Fast burn | **14.4x** | 1h and 5m | critical (page) | ~2% of 30-day budget |
| Slow burn | **6x** | 6h and 30m | warning (ticket) | ~5% of 30-day budget |

Concrete thresholds on the bad-ratio:

| SLO | Fast-burn threshold (14.4 × budget) | Slow-burn threshold (6 × budget) |
|-----|-------------------------------------|----------------------------------|
| Availability (budget 0.001) | 5xx ratio > **1.44%** | 5xx ratio > **0.6%** |
| Latency (budget 0.01) | slow ratio > **14.4%** | slow ratio > **6%** |

## Additional operational alerts

These aren't burn-rate alerts but catch failure modes an SLO burn might detect too
slowly (or not at all when there's no traffic):

| Alert | Fires when | Severity |
|-------|-----------|----------|
| `EdgeCacheServiceDown` | no edge-cache replicas are up | critical |
| `EdgeCacheHitRatioCollapsed` | hit ratio < 50% under real traffic | warning |
| `EdgeCacheRedisDown` | `redis_up == 0` (cache layer unreachable) | critical |

## Where this lives

- Recording + alerting rules: [`k8s/slo-rules.yaml`](../k8s/slo-rules.yaml) (a `PrometheusRule`).
- Runbooks (one per alert): [`docs/runbooks/`](./runbooks/).
- Dashboards: the "Edge Cache — Golden Signals + Cache" Grafana dashboard.
