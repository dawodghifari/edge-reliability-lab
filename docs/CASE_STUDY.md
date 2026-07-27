# I built an edge cache, set SLOs, and broke it on purpose

I wanted to understand site reliability engineering by *practising* it, not just reading
the Google SRE book, so I built a small but complete system: instrument it, define what
"reliable enough" means, alert on that, then break it under load and write up the
incident. This is what happened — including the two bugs that only showed up when I tried
to break it.

## The system

`edge-cache` is a mock edge node for video delivery. Clients request "segments"; the
service returns them from a **Redis** cache on a hit, or fetches from a simulated
**origin** (a deliberate ~150 ms latency) on a miss and repopulates the cache. It's a
FastAPI app on **Kubernetes** (a local `kind` cluster), with **Prometheus** scraping its
metrics and **Grafana** showing the four golden signals plus the one that actually matters
for a cache: hit ratio.

That last point became the theme of the whole project. **A cache doesn't fail with
errors — it fails with latency.** When the cache goes away, everything still returns
`200`; it just gets slow as requests fall through to the origin. An availability-only
dashboard would call that "fine." So the SLOs had to measure latency, not just success.

## Defining "reliable enough"

I set two SLOs on the user-facing endpoint: **99.9% of requests non-5xx** (availability)
and **99% of requests under 200 ms** (latency), each with an error budget. Then I wrote
**multi-window, multi-burn-rate** alerts from the SRE workbook: page when the budget burns
at 14.4× over both a 1-hour and a 5-minute window; ticket at 6× over 6 hours and 30
minutes. Requiring a long *and* a short window to agree gives fast detection without
flapping on every blip.

I also added faster **operational** alerts — "Redis is unreachable", "hit ratio
collapsed" — because a 1-hour burn window is too slow to catch a sharp, short outage.
Having both kinds turned out to matter.

## Breaking it

The experiment: with steady load from **k6**, scale Redis to zero and watch. The intended
story was clean — hit ratio collapses, latency climbs, `redis_up` drops, alerts fire,
restore Redis, recover. I wrote the post-mortem template ahead of time.

The reality was more interesting.

### Bug #1: the service stalled instead of degrading

On the first real run, throughput cratered to near zero and the hit-ratio alert never
fired. The cause was subtle: `/segment` is a **synchronous** endpoint, so every request
runs on a worker thread. With Redis down, each request *blocked* on the dead connection
before falling through to origin. Worker threads piled up faster than they drained, the
service stopped completing requests, and the hit/miss counters — which the alert reads —
stopped advancing. The outage had made itself *invisible*. The exact opposite of the goal.

The fix is a classic pattern: a **circuit breaker**. After a few consecutive Redis
failures it "opens" and the app stops calling Redis entirely for a cooldown, failing fast
to origin. Threads stay free, throughput holds, and the hit ratio collapses cleanly the
way the alert expects. Crucially, it keeps the outage *visible* — `redis_up` stays 0 and a
new `redis_circuit_state` gauge shows the breaker is open — so it drops the stall, not the
signal. Every few seconds it lets one request through as a half-open probe; when Redis
returns, that probe closes the breaker automatically.

### Bug #2: the alert that cried "pending"

With the breaker in, I re-ran the incident. This time the service held ~128 rps and
served ~69,000 misses through the fault — degrading gracefully, exactly as intended. But
the hit-ratio alert *still* didn't fire; it sat at PENDING.

This one nearly fooled me. The Grafana panel showed traffic near zero, which looked like
the stall again. It wasn't. On a 3,000-rps axis, ~128 rps is visually indistinguishable
from zero. The **counter** told the truth: 69,000 misses means requests were absolutely
flowing. The real problem was that the alert's 5-minute averaging window meant the hit
ratio didn't even cross 50% until ~5 minutes into a 9-minute fault, and the extra "hold
for 5 minutes" condition couldn't complete before Redis came back. The alert was simply
too slow. I retuned it to a 2-minute window and a 2-minute hold — a *fast* operational
alert, distinct from the deliberately-slow burn-rate alerts — and on the next run it fired
at ~4 minutes, comfortably inside the fault.

The lesson I keep: **trust instrumented counters over eyeballed graphs.** An axis scale
almost sent me debugging a stall that no longer existed.

## Planning for the next outage

With the incident understood, I modelled capacity. The interesting number isn't normal
throughput (~3,000 rps across two replicas, CPU-bound) — it's **degraded** throughput.
During an outage every request is a 150 ms origin fetch holding a worker thread, so a
replica sustains only ~267 rps before threads saturate. Two replicas → ~530 rps of
outage-mode capacity. That's what the circuit breaker gives capacity planning: it makes
degraded capacity a *finite, plannable number* instead of "zero, because everything
stalls." The planning conclusion is sharp — surviving a Redis outage *at peak* needs far
more replicas than serving normal traffic does, which reframes how many replicas "enough"
really is.

## Adding the third pillar: tracing

Metrics and alerts answered *is the service breaching its SLO* and *how often do we miss
cache*. Neither answered *why was this particular request slow*. So I added distributed
tracing — OpenTelemetry spans exported to **Grafana Tempo**, rendered next to the existing
Prometheus data.

A `/segment` request now decomposes into `cache.lookup` → `origin.fetch` → `cache.write`,
with the Redis call auto-instrumented inside the lookup. The payoff showed up immediately
when I re-ran the outage drill: during the fault, the Redis spans **disappear entirely**.
`cache.lookup` drops from 142µs-with-a-Redis-GET to 27µs-with-nothing, and the trace falls
from eight spans to six. The circuit breaker isn't calling a dependency it knows is dead —
which I'd previously only been able to infer from a gauge, and can now simply see.

### Bug #3: exemplars pointing at traces that didn't exist

Exemplars are the metric-to-trace link — a trace id attached to a histogram observation,
so a latency spike becomes a click-through to the request behind it. I wired them up, the
diamonds appeared on the p99 panel, I clicked one, and Tempo said `failed to get trace
with id ... Status: Not Found`.

The cause: an **unsampled span still has a valid trace id**. It just never gets exported.
My helper returned the id unconditionally, so at a 10% sample ratio roughly nine in ten
exemplars linked to traces Tempo had never received. The fix is one condition —
`ctx.trace_flags.sampled` — plus two regression tests.

What made this worth writing down is that nothing *looked* broken. Grafana rendered the
diamond, the click navigated, the id was well-formed. It only failed at the destination,
and only once sampling was on: at 100% sampling every link resolves and the bug is
invisible. It needed a production-shaped configuration to surface at all.

It's the same species of error as the Grafana axis that made 128 rps look like zero — the
tooling stated something confidently and wrongly. A dead link is worse than no link,
because it teaches you to distrust the instruments during the incident when you need them.

### Verification run

Re-running the drill with tracing enabled: **896,585 requests over 17 minutes at ~879
rps, zero failures**, 92% cache hit ratio, median 3.9ms, p95 154ms. The percentile spread
maps cleanly onto the two request shapes the traces show — the median is the cache-hit
path, p95 is the miss path paying the ~150ms origin fetch.

Worth stating plainly: at 20 VUs and a 21.4ms mean iteration, the load generator caps
around 930 rps, so this run was client-bound, not service-bound. It demonstrates that
tracing didn't destabilise anything under sustained load and that the breaker still
delivered zero errors through a full cache outage. It does not measure capacity, and it
doesn't isolate tracing overhead — that needs a fixed-load A/B at sample ratios 0 and 0.1,
which I haven't run yet.

## Making it reproducible

Finally, I made the whole thing rebuildable: **Terraform** manages the monitoring stack
(via Helm) and applies the app's existing manifests, so `terraform apply` recreates the
in-cluster state from scratch; and **GitHub Actions** lints, tests, and builds the image on
every push. The Kubernetes YAML stays the single source of truth — Terraform just applies
it — so there's no drift between what I tested and what deploys.

## What I took away

- A cache's failure mode is latency; measure it, or you won't see the outage.
- Don't hammer a dependency you already know is down — bound it and fail fast.
- Operational alerts and budget-burn alerts want *different* time windows.
- Health checks must not hard-depend on the thing they might be reporting on (an early
  version's live Redis ping in `/healthz` turned a blip into a correlated outage — fixed
  by making the probe report the last-observed health instead).
- When a graph and a counter disagree, believe the counter.
- Metrics tell you the service is slow; only traces tell you which leg of the request
  consumed the time.
- Test observability under its production configuration. Sampling, batching and retention
  are exactly where the silent failures live — the exemplar bug was invisible until the
  sample ratio dropped below 1.0.

The code, dashboards, runbooks, the full post-mortem, and the capacity model are all in
the repo. It's a lab, but every failure in it was real, found the hard way, and fixed.
