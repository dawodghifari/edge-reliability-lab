#!/usr/bin/env bash
# Generate a little HTTP traffic against the edge-cache so the Grafana panels move.
# Requests come from a small pool of segment ids, so the cache warms and the
# hit-ratio panel climbs over time.
#
#   bash scripts/generate-traffic.sh            # ~60s of traffic
#   DURATION=120 RPS=20 bash scripts/generate-traffic.sh
#
# If nothing is already listening on localhost:8000, the script starts its own
# port-forward to svc/edge-cache and tears it down on exit.
set -euo pipefail

APP_NS="edge-lab"
BASE_URL="${BASE_URL:-http://localhost:8000}"
DURATION="${DURATION:-60}"     # seconds
RPS="${RPS:-15}"               # approximate requests per second
POOL_SIZE="${POOL_SIZE:-20}"   # number of distinct segment ids in rotation

PF_PID=""
cleanup() { [[ -n "$PF_PID" ]] && kill "$PF_PID" 2>/dev/null || true; }
trap cleanup EXIT

if ! curl -s -o /dev/null --max-time 1 "$BASE_URL/healthz"; then
  echo "→ Nothing on $BASE_URL; starting a port-forward to svc/edge-cache ..."
  kubectl -n "$APP_NS" port-forward svc/edge-cache 8000:8000 >/dev/null 2>&1 &
  PF_PID=$!
  sleep 3
fi

echo "→ Sending ~${RPS} req/s for ${DURATION}s across ${POOL_SIZE} segment ids ..."
sleep_between=$(awk "BEGIN { printf \"%.3f\", 1.0/${RPS} }")
end=$(( $(date +%s) + DURATION ))
count=0
hits=0
while [[ $(date +%s) -lt $end ]]; do
  id="seg-$(( RANDOM % POOL_SIZE ))"
  xcache=$(curl -s -o /dev/null -D - "$BASE_URL/segment/$id" | awk -F': ' 'tolower($1)=="x-cache"{gsub(/\r/,"",$2); print $2}')
  count=$((count + 1))
  [[ "$xcache" == "HIT" ]] && hits=$((hits + 1))
  sleep "$sleep_between"
done

echo "✓ Done. Sent ${count} requests, ${hits} served from cache."
echo "  Check the Grafana dashboard — latency, RPS, errors, and cache hit ratio should be live."
