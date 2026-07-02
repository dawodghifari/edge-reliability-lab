# Runbook — EdgeCacheHitRatioCollapsed

**Alert:** `EdgeCacheHitRatioCollapsed` (warning)
**Fires when:** cache hit ratio < 50% (2m window) while there is real `/segment` traffic, held for 2 minutes. Tuned as a *fast* operational alert — fires ~4 min into a cache outage.

## What it means
Most requests are missing the cache and hitting the (slow) origin. Origin load and
tail latency climb; if it persists, the latency SLO burns and the origin can saturate.

> **If the ratio looks _frozen_ rather than falling during a Redis outage**, that was
> the pre-breaker failure mode: sync workers stalled on dead Redis, so the hit/miss
> counters stopped advancing and this alert couldn't fire. The circuit breaker now
> prevents that — during a Redis outage `redis_circuit_state` shows **2 (open)** and the
> ratio collapses cleanly. A frozen ratio today means look at throughput/thread saturation.

## First checks (≈2 min)
- Grafana: "Cache hit ratio" panel is low; "Origin fetch latency" and p99 are up;
  "Redis up" — is it still UP? Check `redis_circuit_state` (2 = breaker open → Redis is down).
- `kubectl -n edge-lab get pods -l app=redis` — Redis healthy?
- Check whether a deploy, restart, or cache flush just happened (a cold cache is
  expected briefly after those).

## Likely causes
- **Cold cache** after a restart/redeploy or a `flush` — usually self-heals as it re-warms.
- Redis down or flapping (see `EdgeCacheRedisDown`).
- Chaos `cache_bypass` left on (`GET /admin/chaos` to check state).
- Traffic pattern shifted to mostly-unique keys (low reuse) — less likely in this lab.

## Mitigation
- If chaos bypass is on: `curl -XPOST localhost:8000/admin/chaos -d '{"reset":true}' -H 'content-type: application/json'`.
- If Redis is down: follow [edge-cache-redis-down](./edge-cache-redis-down.md).
- If just a cold cache: confirm the ratio is climbing back on its own; no action needed.

## Verify recovery
- Hit ratio climbs back toward its normal (~0.9+); origin latency and p99 fall. Alert
  clears after the ratio holds above 50%.
