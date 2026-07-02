# Runbook — EdgeCacheAvailability{Fast,Slow}Burn

**Alerts:** `EdgeCacheAvailabilityFastBurn` (critical), `EdgeCacheAvailabilitySlowBurn` (warning)
**SLO:** 99.9% of `/segment` requests non-5xx (error budget 0.1%).
**Fires when:** the 5xx ratio exceeds the burn threshold on **both** windows at once —
fast: >1.44% over 1h and 5m; slow: >0.6% over 6h and 30m.

## What it means
The service is returning 5xx errors fast enough to threaten the monthly availability
budget. Fast burn = page now; slow burn = investigate before it becomes a page.

## First checks (≈2 min)
- Grafana: "Errors — 5xx rate (%)" — how high, and climbing? Which started it?
- `kubectl -n edge-lab get pods` — restarts, crash-loops, OOM?
- `kubectl -n edge-lab logs deploy/edge-cache --tail=100` — what error is being thrown?
- Rule out an injected fault: `curl -s localhost:8000/admin/chaos` (via port-forward) —
  is `error_rate` non-zero?

## Likely causes
- Injected chaos error rate left on (lab).
- A bad rollout (new image throwing 5xx) — correlate with deploy time.
- A downstream/dependency failure surfacing as 5xx.
- Resource exhaustion (OOM/CPU throttling) causing failures under load.

## Mitigation
- Chaos on: `curl -XPOST localhost:8000/admin/chaos -d '{"reset":true}' -H 'content-type: application/json'`.
- Bad rollout: `kubectl -n edge-lab rollout undo deploy/edge-cache`.
- Overload: scale out `kubectl -n edge-lab scale deploy/edge-cache --replicas=3` and/or
  shed load; investigate the root cause.

## Verify recovery
- 5xx rate returns to ~0 on the dashboard; the burn-rate recording metrics fall back
  under threshold; alert clears (fast: after ~2m stable; the long window relaxes over 1h).

## After the incident
Write a blameless post-mortem (see `docs/postmortems/`), and note budget consumed.
