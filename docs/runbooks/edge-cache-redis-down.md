# Runbook — EdgeCacheRedisDown

**Alert:** `EdgeCacheRedisDown` (critical)
**Fires when:** `redis_up == 0` for 1 minute (the cache layer is unreachable).

## What it means
The edge-cache app cannot reach Redis. In strict mode (the cluster default,
`CACHE_MEMORY_FALLBACK=false`) every request now falls through to the simulated
origin: latency rises and the cache hit ratio collapses. The service still serves,
but slowly — expect the latency SLO to start burning.

**Circuit breaker (expected behaviour):** after a few consecutive Redis failures the
app's breaker **opens** and requests skip Redis entirely, failing fast to origin
instead of stalling a worker thread on the dead dependency. On the dashboard
`redis_circuit_state` goes to **2 (open)**. This is by design — it keeps the outage
visible (`redis_up` stays 0, so this alert still fires) while preventing a thread-pool
stall. Every ~5s one request is allowed through as a half-open probe; when Redis
returns, a probe succeeds, the breaker closes (`redis_circuit_state` → 0), and caching
resumes automatically.

## First checks (≈2 min)
- Grafana: "Redis up" stat shows **DOWN**; `redis_circuit_state` = **2 (open)**; cache
  hit ratio falling; origin-fetch latency and p99 rising.
- `kubectl -n edge-lab get pods -l app=redis` — is the Redis pod `Running`?
- `kubectl -n edge-lab logs deploy/redis` — OOM, evicted, or connection errors?

## Likely causes
- Redis pod deleted, crashed, or OOM-killed.
- Redis Service/endpoint broken (DNS or selector mismatch).
- Network policy or node issue between app and Redis.

## Mitigation
- If the pod is down: `kubectl -n edge-lab rollout restart deploy/redis` (or let it
  restart) and wait for `Running`.
- Confirm the Service resolves: from an app pod,
  `kubectl -n edge-lab exec deploy/edge-cache -- python -c "import socket; print(socket.gethostbyname('redis'))"`.
- The app auto-reconnects on the next request (lazy reconnect) — no app restart needed.

## Verify recovery
- "Redis up" returns to **UP**; hit ratio climbs back as the cache re-warms; p99 drops.
  Alert clears within ~1 min.

## Note
This is the failure the chaos experiment induces on purpose to exercise the incident
process (see `docs/postmortems/`).
