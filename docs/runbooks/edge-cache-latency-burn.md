# Runbook — EdgeCacheLatency{Fast,Slow}Burn

**Alerts:** `EdgeCacheLatencyFastBurn` (critical), `EdgeCacheLatencySlowBurn` (warning)
**SLO:** 99% of `/segment` requests < 200ms (error budget 1%).
**Fires when:** the fraction of requests slower than 200ms exceeds the burn threshold on
**both** windows — fast: >14.4% over 1h and 5m; slow: >6% over 6h and 30m.

## What it means
Too many requests are slow. Users experience buffering/stalls even if nothing is
erroring. The latency budget is burning.

## First checks (≈2 min)
- Grafana: "Latency (p50/p99)" — is p99 above 200ms? "Cache hit ratio" — has it dropped
  (more slow origin fetches)? "Saturation — CPU & in-flight" — are we saturated?
- `kubectl -n edge-lab get pods` and `kubectl top pods -n edge-lab` (if metrics available).

## Likely causes
- **Cache hit ratio dropped** → more slow origin fetches (cold cache / Redis issue).
  See [hit-ratio-collapsed](./edge-cache-hit-ratio-collapsed.md) and
  [redis-down](./edge-cache-redis-down.md).
- Injected chaos latency (`curl -s localhost:8000/admin/chaos` → `latency_seconds` > 0).
- CPU saturation / throttling under load (too few replicas).
- Origin latency genuinely up (in this lab, `ORIGIN_LATENCY_SECONDS`).

## Mitigation
- Chaos on: `curl -XPOST localhost:8000/admin/chaos -d '{"reset":true}' -H 'content-type: application/json'`.
- Saturated: scale out `kubectl -n edge-lab scale deploy/edge-cache --replicas=3`.
- Redis/cache issue: follow the linked runbooks to restore the hit ratio.

## Verify recovery
- p99 back under 200ms; slow-ratio recording metrics fall under threshold; alert clears.
