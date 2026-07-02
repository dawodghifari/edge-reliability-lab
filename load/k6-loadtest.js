// k6 load test for the edge-cache service.
//
//   # hits the NodePort directly (kind maps host:30080 -> node:30080), NO port-forward
//   # needed — kubectl port-forward can't carry this much traffic:
//   k6 run load/k6-loadtest.js
//
// Tunables (env vars):
//   BASE_URL   default http://localhost:30080  (the edge-cache-lb NodePort)
//   POOL_SIZE  distinct segment ids in rotation (default 20 → warms cache, high hit ratio)
//   VUS        peak virtual users (default 20)
//   HOLD       steady-state duration (default 13m) — long enough to run an incident inside it
//
// k6 reports p99 latency (http_req_duration) and error rate (http_req_failed) at the end,
// which is what the plan asks us to capture. Thresholds are intentionally loose so the run
// does NOT abort while we deliberately break things mid-test.

import http from 'k6/http';
import { check } from 'k6';
import { Rate, Trend } from 'k6/metrics';

const BASE = __ENV.BASE_URL || 'http://localhost:30080';
const POOL = parseInt(__ENV.POOL_SIZE || '20', 10);
const VUS = parseInt(__ENV.VUS || '20', 10);
const HOLD = __ENV.HOLD || '15m';

// Custom metrics for the write-up.
const cacheHitRate = new Rate('cache_hit_rate');       // fraction of responses served from cache
const segmentLatency = new Trend('segment_latency_ms', true);

export const options = {
  stages: [
    { duration: '1m', target: VUS }, // ramp up + warm the cache
    { duration: HOLD, target: VUS },  // steady state — induce the incident during this window
    { duration: '1m', target: 0 },    // ramp down
  ],
  thresholds: {
    // Informational only — `abortOnFail` is not set, so a breach won't stop the run.
    http_req_duration: ['p(99)<2000'],
    http_req_failed: ['rate<0.5'],
  },
};

export default function () {
  const id = `seg-${Math.floor(Math.random() * POOL)}`;
  const res = http.get(`${BASE}/segment/${id}`);

  check(res, {
    'status is 200': (r) => r.status === 200,
  });

  // Record cache hit/miss from the X-Cache header the app sets.
  const xcache = res.headers['X-Cache'];
  if (xcache) cacheHitRate.add(xcache === 'HIT');
  segmentLatency.add(res.timings.duration);
}
