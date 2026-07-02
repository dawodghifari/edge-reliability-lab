#!/usr/bin/env bash
# Phase 4 — induce a CONTROLLED incident: a Redis (cache-layer) outage under load.
#
# What happens: with load running, we scale Redis to 0. In strict cache mode every
# request now misses and falls through to the (slow) origin — hit ratio collapses,
# latency spikes, redis_up drops. This is the classic edge-cache failure: a cold
# cache overloading the origin. We hold the fault, then mitigate by bringing Redis
# back, and watch the service recover.
#
# Prereqs (each in its own terminal, left running):
#   1) k6 run load/k6-loadtest.js         (hits the edge-cache-lb NodePort on :30080,
#                                          no port-forward needed)
#   2) Grafana open on the "Edge Cache — Golden Signals + Cache" dashboard
#
# Usage:
#   bash scripts/run-incident.sh
#   FAULT_SECONDS=360 RECOVERY_SECONDS=180 bash scripts/run-incident.sh
#
# It prints a timestamped timeline and also saves it to docs/postmortems/ so you can
# paste real times straight into the post-mortem.
set -euo pipefail

NS="edge-lab"
FAULT_SECONDS="${FAULT_SECONDS:-540}"       # 9 min — the hit-ratio 5m average needs ~2.5m to cross
                                            # 50%, then HitRatioCollapsed's for:5m → fires ~7.5m in
RECOVERY_SECONDS="${RECOVERY_SECONDS:-150}" # 2.5 min of recovery observation
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$REPO_ROOT/docs/postmortems/incident-$(date +%Y%m%d-%H%M%S)-timeline.txt"

ts()  { date '+%Y-%m-%d %H:%M:%S %Z'; }
log() { printf '%s  %s\n' "$(ts)" "$*" | tee -a "$OUT"; }

mkdir -p "$(dirname "$OUT")"

cat <<'EOF'
============================================================
  CONTROLLED INCIDENT: Redis outage under load
------------------------------------------------------------
  Make sure load is running and Grafana is open BEFORE you
  continue. You will screenshot the dashboard during the
  fault and during recovery.
============================================================
EOF
read -r -p "Press Enter to begin the incident (Ctrl-C to abort)... " _

log "INCIDENT START — baseline healthy, load running."
log "FAULT INJECTED — scaling deploy/redis to 0 (cache layer down)."
kubectl -n "$NS" scale deploy/redis --replicas=0

log "Holding fault for ${FAULT_SECONDS}s."
log ">>> WATCH & SCREENSHOT Grafana now: 'Redis up' -> DOWN, cache hit ratio -> 0,"
log ">>> latency p99 climbing, origin-fetch latency up."
log ">>> Expected alerts: EdgeCacheRedisDown (~1m), EdgeCacheHitRatioCollapsed (~5m)."
sleep "$FAULT_SECONDS"

log "MITIGATION — scaling deploy/redis back to 1."
kubectl -n "$NS" scale deploy/redis --replicas=1
kubectl -n "$NS" rollout status deploy/redis --timeout=120s
log "REDIS READY — cache is cold and will re-warm as traffic repopulates it."

log "Observing recovery for ${RECOVERY_SECONDS}s."
log ">>> WATCH & SCREENSHOT recovery: 'Redis up' -> UP, hit ratio climbing back,"
log ">>> latency returning to normal, alerts clearing."
sleep "$RECOVERY_SECONDS"

log "INCIDENT END — service recovered."
echo
echo "Timeline saved to: $OUT"
echo "Use these timestamps in docs/postmortems/2026-07-01-redis-cache-outage.md"
