# Frontend observability

Web Vitals, errors and API timings from the Vercel-hosted SPA, in the same
Grafana as the backend, correlated by request id.

## Why it looks like this

The frontend is **Vite + React with react-router-dom**. There is no Next.js, so
there are no Server Components, Server Actions, Edge Functions, Vercel
middleware or API routes to instrument. Vercel serves static assets; there is no
server-side code there at all, which is also why Log Drains would have nothing
useful to drain.

Two facts about the existing stack decided the architecture:

1. **Prometheus pulls.** You cannot scrape a browser.
2. **The Loki push endpoint is behind basic auth.** Putting that credential in
   client JavaScript would turn the log store into an open write proxy for
   anyone who opened devtools.

So the browser needs a server-side intermediary, and the API already is one.

## Data flow

```
Browser (Vite SPA on Vercel)
  │  ui/src/observability - batched, sampled, sanitised
  │  sendBeacon on hide, fetch(keepalive) otherwise
  ▼
POST /api/telemetry                     performance-tracker API (EC2)
  │  rate limited, 64kb cap, allowlist schema
  ├─► prom-client ──► /metrics ──► Prometheus  (existing 15s scrape)
  └─► pino ──► PM2 log file ──► Alloy ──► Loki (existing push)
                                            ▼
                                         Grafana
```

**Nothing new runs anywhere.** No collector, no Faro receiver, no Tempo, no
Pushgateway, no new public endpoint, no new credential.

## Correlation with the backend

`middleware/requestLogger.middleware.js` already reads `x-request-id` from the
client, falls back to generating one, echoes it on the response and puts it on
every log line. The axios instrumentation sends that header, so:

1. find a failing call on **Frontend Observability → Logs**
2. copy its `request_id`
3. search it on **Performance Tracker Logs**

Same id, both sides, no tracing infrastructure.

## What is collected, and what is deliberately not

| Collected | Where |
| --- | --- |
| LCP, INP, CLS, FCP, TTFB | `frontend_web_vitals_seconds`, `frontend_cls` |
| JS errors, unhandled rejections, chunk/resource load failures | `frontend_errors_total` + Loki |
| API call duration, status class, timeouts, aborts | `frontend_api_request_duration_seconds` |
| Route activations | `frontend_page_views_total` |
| Deployed build | `frontend_build_info` |

**Not collected: product analytics.** PostHog already owns page views, funnels
and identity. Duplicating it here would double the cost and split the answer
across two tools.

## Cardinality

Every Prometheus label is bounded by construction:

| Label | Domain |
| --- | --- |
| `route`, `endpoint` | normalised, ids collapsed to `:id` / `:email` / `:token`, query string dropped |
| `device` | desktop, mobile, tablet |
| `browser` | chrome, safari, firefox, edge, opera, samsung, other |
| `environment` | production, preview, staging, development |
| `status_class` | 2xx, 4xx, 5xx, unknown |
| `metric` | LCP, INP, CLS, FCP, TTFB |

**Never labels**: full URL, error message, stack, user id, session id, request
id, trace id, query string, deploy version. Those that are needed travel as
structured log fields instead.

The deploy version is a label on `frontend_build_info` only. At roughly 17
deploys a day, putting the release on the vitals histograms would churn the
series set hard; deploy correlation is done with the annotation and with log
fields.

## Sampling and volume control

- **Session sampling**, not event sampling (`VITE_OBSERVABILITY_SAMPLE_RATE`,
  default 1). A sampled-in session reports everything, so one user's story stays
  whole instead of a funnel full of holes.
- **Errors ignore sampling.** A rare error is the one you need.
- **Error deduplication**: the same fingerprint is reported at most 5 times per
  tab. A render loop throwing 60 times a second would otherwise be a
  self-inflicted denial of service.
- **Batching**: flushed every 10s, at 50 events, or on tab hide.
- **Server-side rate limit**: 120 requests/min per IP, 64kb body, 50 events per
  batch. Rejections are counted in `frontend_telemetry_events_dropped_total`.

## Sanitisation

Applied in the browser and again on the server, because the server cannot trust
a browser:

- JWTs, `Bearer` tokens, emails, long opaque strings and query-string values are
  redacted from messages and stacks
- stacks are stripped of origins and clamped to 2000 chars
- unknown event kinds, vitals and enum values are dropped, not coerced

Covered by `backend/__tests__/telemetryIngest.test.js` (32 tests), which asserts
on the redactions and on the label domains rather than on implementation.

## Environment variables

Frontend (Vercel, all `VITE_` and none secret):

| Variable | Default | Purpose |
| --- | --- | --- |
| `VITE_OBSERVABILITY_ENABLED` | on outside dev | force on/off |
| `VITE_OBSERVABILITY_ENV` | derived | override the environment label |
| `VITE_OBSERVABILITY_SAMPLE_RATE` | `1` | session sampling, 0..1 |
| `VITE_APP_VERSION` | `unknown` | shown on `frontend_build_info` |
| `VITE_VERCEL_ENV` | - | Vercel system variable, enable in project settings |
| `VITE_VERCEL_GIT_COMMIT_SHA` | - | Vercel system variable, enable in project settings |

There is **no secret in the browser**. The ingest endpoint needs no token; it is
protected by the CORS allowlist, the rate limit and the schema.

Backend: none. It reuses the existing logger and metrics registry.

## Vercel setup

1. Project → Settings → Environment Variables → enable the system variables so
   `VITE_VERCEL_ENV` and `VITE_VERCEL_GIT_COMMIT_SHA` are exposed to the build.
2. Set `VITE_APP_VERSION` if you want something friendlier than the SHA.
3. Preview deployments report `environment="preview"` automatically and are
   filtered out of the production dashboard by the `environment` variable.

## Dashboard

`grafana/dashboards/frontend-observability.json`, uid `frontend-observability`,
provisioned by the existing file provider. Filters: environment, route, device,
browser. Sections: Overview, Web Vitals, API calls, Errors, Logs.

The **Frontend deploys** annotation fires when a new `frontend_build_info`
series appears, which is the moment a build reaches a real browser rather than
the moment Vercel finished building it. That is the more useful instant for
correlating a regression.

## Suggested alerts, none enabled

Enable after a week of baseline. Thresholds below are starting points.

| Alert | Query | Threshold | Window | Notes |
| --- | --- | --- | --- | --- |
| Error rate spike | `sum(rate(frontend_errors_total[5m])) * 60` | > 3x the 24h median | 10m | Noisy on low traffic; require a floor of ~5 errors/min |
| API failures | `sum(rate(frontend_api_request_duration_seconds_count{status_class=~"5xx\|unknown"}[5m])) / sum(rate(frontend_api_request_duration_seconds_count[5m]))` | > 5% | 10m | `unknown` includes client network loss, so this can fire on a bad ISP |
| LCP regression | `histogram_quantile(0.75, sum by (le) (rate(frontend_web_vitals_seconds_bucket{metric="LCP"}[30m])))` | > 4s | 30m | Long window on purpose: LCP is per-page-load and noisy |
| INP regression | same with `metric="INP"` | > 0.5s | 30m | |
| Chunk load failures | `sum(rate(frontend_errors_total{kind="chunk_load"}[5m])) * 60` | > 1/min for 15m | 15m | Expect a burst for a few minutes after every deploy; alert only if sustained |
| Traffic drop | `sum(rate(frontend_page_views_total[10m]))` | < 30% of same time last week | 15m | Will fire on holidays |
| Ingest rejecting | `sum(rate(frontend_telemetry_events_dropped_total{reason!="rate_limited"}[10m]))` | > 0 sustained | 15m | Means a client is sending something the schema refuses: usually a stale build |

## Cost and volume

Qualitatively: a page load produces 5 vitals + 1 page view + one event per API
call, batched into roughly one 2-10kb request every 10 seconds per active tab.
Prometheus gains ~6 metric families whose series count is the product of bounded
label sets, so it grows with the number of distinct routes, not with traffic.
Loki only receives errors and failed API calls, not successful ones, which is
what keeps this from doubling the log bill.

Vercel cost is unchanged: no functions, no middleware, no extra bandwidth beyond
the telemetry POSTs, which go to the API rather than through Vercel.

## Rollback

Fully reversible in three independent steps, smallest first:

1. `VITE_OBSERVABILITY_ENABLED=false` in Vercel and redeploy. Collection stops;
   nothing else changes.
2. Revert the frontend commit. The API endpoint keeps working and simply stops
   receiving.
3. Revert the backend commit to remove `/api/telemetry`. The browser's posts
   then 404, which the transport ignores by design.

The dashboard can be deleted independently; nothing depends on it.

## Known limitations

1. **No traces.** There is no Tempo and no OTel anywhere in this stack, so
   correlation is by request id, not by span. Good enough to join a frontend
   call to its backend logs; not enough to see time spent inside the backend
   broken down by span.
2. **No source maps.** Stacks are minified. Uploading source maps needs a
   private store and a symbolication step, which was out of scope; the
   fingerprint groups errors regardless.
3. **`unknown` status conflates causes.** CORS rejection, DNS failure and a
   dropped connection all arrive as status 0.
4. **Web Vitals are page-load scoped.** In an SPA, route changes after the first
   load do not produce a new LCP. Route-level LCP therefore reflects entry
   points, not every navigation.
5. **The ingest endpoint is unauthenticated.** Deliberate, since the errors most
   worth seeing happen before login. It is constrained by CORS, a rate limit, a
   body cap and an allowlisting schema rather than by a token. Someone
   determined could still post plausible-looking events from a browser on an
   allowed origin.
6. **Not yet validated against real traffic.** Everything below was verified;
   the first production deploy is what confirms the volume estimates.

## What was verified

- 32 unit tests on normalisation, sanitisation and fingerprinting
- an end-to-end post of a realistic batch through the real route, asserting the
  resulting metric labels and that no raw id, email, token or session id
  appears anywhere in `/metrics`
- all 42 dashboard queries executed against the live Prometheus and Loki
- frontend build and its 827 tests, backend suite of 3446 tests
