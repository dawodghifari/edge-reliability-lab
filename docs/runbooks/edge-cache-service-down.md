# Runbook — EdgeCacheServiceDown

**Alert:** `EdgeCacheServiceDown` (critical)
**Fires when:** no `edge-cache` Prometheus targets are `up` for 1 minute.

## What it means
Every edge-cache replica is unreachable or gone. Users cannot fetch segments — this
is a full outage of the service.

## First checks (≈2 min)
- Grafana → "Edge Cache — Golden Signals + Cache": Traffic (RPS) has dropped to 0.
- `kubectl -n edge-lab get pods` — are the `edge-cache` pods `Running`, `CrashLoopBackOff`,
  `Pending`, or gone?
- `kubectl -n edge-lab get deploy edge-cache` — how many replicas are desired vs available?

## Likely causes
- Deployment scaled to 0 (intentionally or by mistake).
- Pods crash-looping (bad image/config) — check `kubectl -n edge-lab logs deploy/edge-cache`.
- Nodes unschedulable / out of resources — `kubectl get nodes`, `kubectl -n edge-lab describe pod <pod>`.

## Mitigation
- If scaled to 0: `kubectl -n edge-lab scale deploy/edge-cache --replicas=2`.
- If crash-looping: read the logs; roll back with `kubectl -n edge-lab rollout undo deploy/edge-cache`.
- If node pressure: free resources or re-create the kind node; confirm pods schedule.

## Verify recovery
- Pods `Running` and `Ready`; Prometheus target back `UP`; RPS returns on the dashboard;
  `curl` through a port-forward returns segments. Alert clears within ~1 min.
