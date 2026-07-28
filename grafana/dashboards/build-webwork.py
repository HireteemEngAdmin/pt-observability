"""Generate the WebWork Integration Monitoring dashboard.

Run from the repo root:  python3 grafana/dashboards/build-webwork.py

Backed by the metrics added in performance-tracker
(backend/utils/webworkMetrics.js) and the pino logs from
backend/utils/webworkLogger.js, which reach Loki with module="webwork".

Two things the panels say out loud rather than implying:

- The client has no retry and no timeout. "Retries" here means the attempts an
  operation actually made — pagination pages, or a caller trying again — not a
  backoff loop, because there is none. Time-waiting-on-retry does not exist as a
  distinct number; the honest equivalent is total time awaiting WebWork.
- Availability is event-driven. webwork_up only moves when a call happens, so a
  quiet period holds the last known state instead of reading as an outage.
"""
import json

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}

# Every metric query is scoped by these so the dashboard variables actually bind.
SEL = 'job=~"$job", endpoint=~"$endpoint", method=~"$method"'
SEL_STATUS = SEL + ', status=~"$status"'

panels = []
pid = 0


def nid():
    global pid
    pid += 1
    return pid


def prom(*exprs, legend=None, instant=False):
    out = []
    for i, e in enumerate(exprs):
        t = {"datasource": PROM, "expr": e, "refId": chr(65 + i), "editorMode": "code",
             "range": not instant, "instant": instant}
        if legend:
            t["legendFormat"] = legend[i] if isinstance(legend, list) else legend
        out.append(t)
    return out


def row(title, y):
    return {"type": "row", "title": title, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "id": nid(), "collapsed": False, "panels": []}


def stat(title, expr, x, y, w=4, h=4, unit="short", desc="", steps=None, legend="",
         mappings=None, dec=None):
    return {
        "type": "stat", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": PROM,
        "targets": prom(expr, legend=legend, instant=True),
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "none", "textMode": "auto"},
        "fieldConfig": {"defaults": {
            "unit": unit, "decimals": dec, "mappings": mappings or [],
            "thresholds": {"mode": "absolute",
                           "steps": steps or [{"color": "text", "value": None}]},
        }, "overrides": []},
    }


def ts(title, exprs, legend, x, y, w=12, h=8, unit="short", desc="", stack=False, dec=None):
    return {
        "type": "timeseries", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": PROM,
        "targets": prom(*exprs, legend=legend),
        "options": {"legend": {"displayMode": "table", "placement": "bottom",
                               "showLegend": True, "calcs": ["mean", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "fieldConfig": {"defaults": {
            "unit": unit, "min": 0, "decimals": dec,
            "custom": {"drawStyle": "line", "lineWidth": 1, "fillOpacity": 20,
                       "showPoints": "never", "spanNulls": True,
                       "stacking": {"mode": "normal" if stack else "none"}},
            "color": {"mode": "palette-classic"},
        }, "overrides": []},
    }


def ranked(title, expr, label, x, y, w=12, h=8, unit="short", desc=""):
    """Top-N bar gauge.

    Prometheus, like Loki, answers with one frame per series and calls the value
    field "Value" in all of them. labelsToFields promotes the label to a real
    field, merge folds the frames into one table, and values:true then draws one
    bar per row. Without this the panel collapses into a single unlabelled bar.
    """
    return {
        "type": "bargauge", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": PROM,
        "targets": prom(expr, instant=True),
        "transformations": [
            {"id": "labelsToFields", "options": {}},
            {"id": "merge", "options": {}},
            {"id": "sortBy", "options": {"sort": [{"field": "Value #A", "desc": True}]}},
            {"id": "organize", "options": {"excludeByName": {"Time": True}}},
        ],
        "options": {"orientation": "horizontal", "displayMode": "gradient",
                    "showUnfilled": True, "valueMode": "text", "namePlacement": "left",
                    "minVizWidth": 8, "minVizHeight": 16, "sizing": "auto",
                    "reduceOptions": {"calcs": [], "fields": "", "values": True}},
        "fieldConfig": {"defaults": {"unit": unit, "min": 0,
                                     "color": {"mode": "continuous-BlPu"}}, "overrides": []},
    }


# Log panels cannot be scoped by $job. The same process is named differently in
# the two systems: Prometheus calls the API process "api" (from prometheus.yml)
# while Loki calls it "server" (Alloy names streams after the PM2 process). $job
# resolves against Prometheus, so {job=~"$job"} matches nothing in Loki for the
# API — silently, since an empty result is a valid query. The selector below uses
# Loki's own values; `module = webwork` is what actually scopes these panels.
def logs(title, expr, x, y, w=24, h=12, desc=""):
    return {
        "type": "logs", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": LOKI,
        "targets": [{"datasource": LOKI, "expr": expr, "refId": "A",
                     "queryType": "range", "editorMode": "code"}],
        "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                    "enableLogDetails": True, "dedupStrategy": "none"},
    }


UP_MAP = [{"options": {"0": {"text": "DOWN", "color": "red"},
                       "1": {"text": "UP", "color": "green"}}, "type": "value"}]

# ── Overview ─────────────────────────────────────────────────────
y = 0
panels.append(row("Overview", y))
y += 1
panels.append(stat(
    "Integration status", 'min(webwork_up{job=~"$job"})', 0, y, w=3,
    desc="Reflects the last call each process actually made. It does not decay with silence: "
         "no traffic holds the last known state instead of reporting an outage. Empty until "
         "the first call after a restart.",
    mappings=UP_MAP,
    steps=[{"color": "red", "value": None}, {"color": "green", "value": 1}]))
panels.append(stat(
    "Since last success", 'time() - max(webwork_last_success_timestamp_seconds{job=~"$job"})',
    3, y, w=3, unit="s",
    desc="Age of the most recent successful call across processes.",
    steps=[{"color": "green", "value": None}, {"color": "orange", "value": 3600},
           {"color": "red", "value": 86400}]))
panels.append(stat(
    "Success rate",
    f'sum(rate(webwork_requests_total{{{SEL}, status=~"2..|3.."}}[$__range])) '
    f'/ sum(rate(webwork_requests_total{{{SEL}}}[$__range]))',
    6, y, w=3, unit="percentunit", dec=2,
    desc="Share of calls answered with 2xx or 3xx over the selected range.",
    steps=[{"color": "red", "value": None}, {"color": "orange", "value": 0.95},
           {"color": "green", "value": 0.99}]))
panels.append(stat(
    "Error rate",
    f'(sum(rate(webwork_errors_total{{{SEL}}}[$__range])) or vector(0)) '
    f'/ sum(rate(webwork_requests_total{{{SEL}}}[$__range]))',
    9, y, w=3, unit="percentunit", dec=2,
    desc="or vector(0) so a clean period renders 0 rather than \"No data\", which would be "
         "indistinguishable from a broken scrape.",
    steps=[{"color": "green", "value": None}, {"color": "orange", "value": 0.01},
           {"color": "red", "value": 0.05}]))
panels.append(stat(
    "Requests / min", f'sum(rate(webwork_requests_total{{{SEL_STATUS}}}[5m])) * 60',
    12, y, w=3, dec=1,
    desc="WebWork's documented ceiling is 60/min per workspace."))
panels.append(stat(
    "Latency p95",
    f'histogram_quantile(0.95, sum by (le) (rate(webwork_request_duration_seconds_bucket{{{SEL}}}[5m])))',
    15, y, w=3, unit="s", dec=2,
    steps=[{"color": "green", "value": None}, {"color": "orange", "value": 2},
           {"color": "red", "value": 5}]))
panels.append(stat(
    "HTTP 429 (range)", f'sum(increase(webwork_rate_limited_total{{endpoint=~"$endpoint"}}[$__range])) or vector(0)',
    18, y, w=3, dec=0,
    desc="Rate-limited responses over the selected range.",
    steps=[{"color": "green", "value": None}, {"color": "red", "value": 1}]))
panels.append(stat(
    "In flight", 'sum(webwork_requests_in_flight{job=~"$job"})', 21, y, w=3, dec=0,
    desc="Calls started and not yet settled. With no timeout in the client, a number that "
         "stays high is the signature of hung requests.",
    steps=[{"color": "green", "value": None}, {"color": "orange", "value": 5},
           {"color": "red", "value": 20}]))
y += 4
panels.append(stat(
    "Consecutive failures", 'max(webwork_consecutive_failures{job=~"$job"})', 0, y, w=3, dec=0,
    desc="Failed calls since the last success, worst process. Rises the moment WebWork starts "
         "refusing and drops to zero on the first success — the fastest signal that the "
         "integration is in trouble.",
    steps=[{"color": "green", "value": None}, {"color": "orange", "value": 3},
           {"color": "red", "value": 10}]))
panels.append(stat(
    "Consecutive successes", 'min(webwork_consecutive_successes{job=~"$job"})', 3, y, w=3, dec=0,
    desc="Successful calls since the last failure, weakest process. Useful to confirm a "
         "recovery actually held rather than flapping.",
    steps=[{"color": "text", "value": None}]))
panels.append(stat(
    "Job: time since last success",
    'time() - max(webwork_job_last_success_timestamp_seconds{job_name=~"$wwjob"} or '
    'webwork_job_last_success_timestamp_seconds)',
    6, y, w=6, unit="s",
    desc="The reconcile job runs nightly at 03:00 UTC, so anything past ~24h means a run was "
         "missed or failed.",
    steps=[{"color": "green", "value": None}, {"color": "orange", "value": 90000},
           {"color": "red", "value": 172800}]))
panels.append(ts(
    "Job runs by status", ['sum by (status) (increase(webwork_job_runs_total[$__interval]))'],
    "{{status}}", 12, y, w=12, h=4, dec=0,
    desc="Terminal status of each execution.", stack=True))

# ── Traffic ──────────────────────────────────────────────────────
y += 4
panels.append(row("Traffic", y))
y += 1
panels.append(ts(
    "Requests over time", [f'sum(rate(webwork_requests_total{{{SEL_STATUS}}}[$__interval]))'],
    "requests/s", 0, y, w=12, unit="reqps",
    desc="Total call rate after the dashboard filters."))
panels.append(ranked(
    "Most called endpoints",
    f'topk(10, sum by (endpoint) (increase(webwork_requests_total{{{SEL_STATUS}}}[$__range])))',
    "endpoint", 12, y, w=12,
    desc="Call count per normalized endpoint over the selected range."))
y += 8
panels.append(ts(
    "Requests by endpoint", [f'sum by (endpoint) (rate(webwork_requests_total{{{SEL_STATUS}}}[$__interval]))'],
    "{{endpoint}}", 0, y, w=12, unit="reqps", stack=True))
panels.append(ts(
    "Requests by status code", [f'sum by (status) (rate(webwork_requests_total{{{SEL_STATUS}}}[$__interval]))'],
    "{{status}}", 12, y, w=12, unit="reqps", stack=True,
    desc="status=\"none\" means the call never got a response — a timeout or a socket failure."))
y += 8
panels.append(ts(
    "Requests by HTTP method", [f'sum by (method) (rate(webwork_requests_total{{{SEL_STATUS}}}[$__interval]))'],
    "{{method}}", 0, y, w=12, unit="reqps", stack=True))
panels.append({
    "type": "heatmap", "title": "Call volume by hour of day", "id": nid(),
    "description": "Hourly totals across the selected range. The reconcile job at 03:00 UTC and "
                   "the morning provisioning rush should stand out as bands.",
    "gridPos": {"h": 8, "w": 12, "x": 12, "y": y}, "datasource": PROM,
    "targets": prom(f'sum(increase(webwork_requests_total{{{SEL_STATUS}}}[1h]))', legend="calls"),
    "options": {"calculate": False,
                "color": {"mode": "scheme", "scheme": "Turbo", "steps": 64},
                "yAxis": {"unit": "short"}, "legend": {"show": True},
                "tooltip": {"show": True, "yHistogram": False},
                "cellGap": 1, "rowsFrame": {"layout": "auto"}},
    "fieldConfig": {"defaults": {"custom": {"hideFrom": {"tooltip": False, "viz": False, "legend": False}}},
                    "overrides": []},
})

# ── Performance ──────────────────────────────────────────────────
y += 8
panels.append(row("Performance", y))
y += 1
panels.append(ts(
    "Latency percentiles",
    [f'histogram_quantile(0.50, sum by (le) (rate(webwork_request_duration_seconds_bucket{{{SEL}}}[$__interval])))',
     f'histogram_quantile(0.90, sum by (le) (rate(webwork_request_duration_seconds_bucket{{{SEL}}}[$__interval])))',
     f'histogram_quantile(0.95, sum by (le) (rate(webwork_request_duration_seconds_bucket{{{SEL}}}[$__interval])))',
     f'histogram_quantile(0.99, sum by (le) (rate(webwork_request_duration_seconds_bucket{{{SEL}}}[$__interval])))',
     f'sum(rate(webwork_request_duration_seconds_sum{{{SEL}}}[$__interval])) '
     f'/ sum(rate(webwork_request_duration_seconds_count{{{SEL}}}[$__interval]))'],
    ["p50", "p90", "p95", "p99", "mean"], 0, y, w=12, unit="s",
    desc="The mean is included because a percentile alone hides a bimodal distribution."))
panels.append(ranked(
    "Slowest endpoints (p95)",
    f'topk(10, histogram_quantile(0.95, sum by (le, endpoint) '
    f'(rate(webwork_request_duration_seconds_bucket{{{SEL}}}[$__range]))))',
    "endpoint", 12, y, w=12, unit="s"))
y += 8
panels.append(ts(
    "Requests in flight", ['sum by (job) (webwork_requests_in_flight{job=~"$job"})'],
    "{{job}}", 0, y, w=8, dec=0,
    desc="Concurrency against WebWork. Sustained growth with no matching request rate means "
         "calls are hanging."))
panels.append(ts(
    "Time spent awaiting WebWork", [f'sum(rate(webwork_waiting_seconds_total[$__interval]))'],
    "seconds per second", 8, y, w=8, dec=2,
    desc="Cumulative wait divided by wall clock. A value above 1 means calls overlap; it is "
         "effectively the average number of calls in flight."))
panels.append(ts(
    "Job duration",
    ['histogram_quantile(0.95, sum by (le, job_name) (rate(webwork_job_duration_seconds_bucket[$__range])))'],
    "{{job_name}} p95", 16, y, w=8, unit="s"))

# ── Rate limit ───────────────────────────────────────────────────
y += 8
panels.append(row("Rate limit", y))
y += 1
panels.append(ts(
    "HTTP 429 over time", ['sum by (endpoint) (increase(webwork_rate_limited_total{endpoint=~"$endpoint"}[$__interval]))'],
    "{{endpoint}}", 0, y, w=12, dec=0, stack=True,
    desc="When these appear tells you which job or rush hit the ceiling."))
panels.append(ranked(
    "Endpoints hit hardest by rate limiting",
    'topk(10, sum by (endpoint) (increase(webwork_rate_limited_total{endpoint=~"$endpoint"}[$__range])))',
    "endpoint", 12, y, w=12))
y += 8
panels.append(ts(
    "Retry-After advertised by WebWork",
    ['histogram_quantile(0.95, sum by (le) (rate(webwork_retry_after_seconds_bucket{endpoint=~"$endpoint"}[$__range])))',
     'sum(rate(webwork_retry_after_seconds_sum{endpoint=~"$endpoint"}[$__range])) '
     '/ sum(rate(webwork_retry_after_seconds_count{endpoint=~"$endpoint"}[$__range]))'],
    ["p95", "mean"], 0, y, w=8, unit="s",
    desc="Only populated when WebWork sends the header. Nothing consumes it yet — the client "
         "has no backoff — so this is the budget a future retry would have to respect."))
panels.append(ts(
    "Attempts per operation",
    [f'histogram_quantile(0.95, sum by (le, outcome) (rate(webwork_operation_attempts_bucket{{endpoint=~"$endpoint"}}[$__range])))'],
    "{{outcome}} p95", 8, y, w=8, dec=0,
    desc="Attempts an operation actually made. Above 1 means pagination, not a retry loop: the "
         "client has no retry. A caller retrying shows up here too."))
panels.append(ts(
    "Operations settled, by outcome",
    ['sum by (outcome) (rate(webwork_operation_attempts_count{endpoint=~"$endpoint"}[$__interval]))'],
    "{{outcome}}", 16, y, w=8, dec=2, stack=True))
y += 8
panels.append(logs(
    "Rate limit events",
    '{job=~"server|cron"} | json | module = `webwork` | rate_limited = `true`', 0, y, h=10,
    desc="pino lines where rate_limited is true, carrying the endpoint, the Retry-After value "
         "and the correlation id."))

# ── Errors ───────────────────────────────────────────────────────
y += 10
panels.append(row("Errors", y))
y += 1
panels.append(ts(
    "Errors over time", [f'sum by (error_type) (rate(webwork_errors_total{{{SEL}}}[$__interval]))'],
    "{{error_type}}", 0, y, w=12, dec=2, stack=True,
    desc="http_4xx / http_5xx / rate_limited / timeout / connection / unexpected."))
panels.append(ranked(
    "Errors by endpoint",
    f'topk(10, sum by (endpoint) (increase(webwork_errors_total{{{SEL}}}[$__range])))',
    "endpoint", 12, y, w=12))
y += 8
panels.append(ts(
    "Transport failures",
    ['sum(increase(webwork_errors_total{error_type="timeout"}[$__interval])) or vector(0)',
     'sum(increase(webwork_errors_total{error_type="connection"}[$__interval])) or vector(0)',
     'sum(increase(webwork_errors_total{error_type="unexpected"}[$__interval])) or vector(0)'],
    ["timeout", "connection", "unexpected"], 0, y, w=12, dec=0, stack=True,
    desc="Failures with no HTTP response. The client sets no timeout, so a \"timeout\" here is "
         "undici's own default rather than a deadline we chose."))
panels.append(ts(
    "Errors by status code",
    [f'sum by (status) (rate(webwork_requests_total{{{SEL}, status=~"4..|5.."}}[$__interval]))'],
    "{{status}}", 12, y, w=12, unit="reqps", stack=True))
y += 8
panels.append(logs(
    "WebWork logs",
    '{job=~"server|cron"} | json | module = `webwork` | outcome =~ `error|failure`', 0, y, h=12,
    desc="module is a field inside the pino JSON, not a Loki stream label — the labels are job, stream, host and filename — so it has to be matched after | json, never as {module=\"webwork\"}, which silently returns nothing. Drop the outcome filter to see successes too."))

# ── Jobs ─────────────────────────────────────────────────────────
y += 12
panels.append(row("Jobs", y))
y += 1
panels.append(stat(
    "Runs in range", 'sum(increase(webwork_job_runs_total[$__range])) or vector(0)',
    0, y, w=4, dec=0))
panels.append(stat(
    "Currently running", 'sum(webwork_job_running)', 4, y, w=4, dec=0,
    desc="The cron is overlap-guarded, so anything above 1 means two processes are running it "
         "at once — expected with multiple instances, worth checking otherwise.",
    steps=[{"color": "green", "value": None}, {"color": "orange", "value": 2}]))
panels.append(stat(
    "Last run", 'time() - max(webwork_job_last_run_timestamp_seconds)', 8, y, w=4, unit="s"))
panels.append(stat(
    "Records processed", 'sum(increase(webwork_job_records_total{outcome="processed"}[$__range])) or vector(0)',
    12, y, w=4, dec=0))
panels.append(stat(
    "Succeeded", 'sum(increase(webwork_job_records_total{outcome="succeeded"}[$__range])) or vector(0)',
    16, y, w=4, dec=0, steps=[{"color": "green", "value": None}]))
panels.append(stat(
    "Failed", 'sum(increase(webwork_job_records_total{outcome="failed"}[$__range])) or vector(0)',
    20, y, w=4, dec=0,
    steps=[{"color": "green", "value": None}, {"color": "red", "value": 1}]))
y += 4
panels.append(ts(
    "Records handled per run", ['sum by (outcome) (increase(webwork_job_records_total[$__interval]))'],
    "{{outcome}}", 0, y, w=12, dec=0, stack=True))
panels.append(ts(
    "WebWork calls per job execution",
    ['histogram_quantile(0.95, sum by (le, job_name) (rate(webwork_job_api_calls_bucket[$__range])))',
     'sum(rate(webwork_job_api_calls_sum[$__range])) / sum(rate(webwork_job_api_calls_count[$__range]))'],
    ["p95", "mean"], 12, y, w=12, dec=0,
    desc="How much WebWork traffic one reconcile run generates — the number to watch against "
         "the 60/min ceiling."))
y += 8
panels.append(logs(
    "Job logs", '{job=~"server|cron"} | json | module = `webwork` | job_execution_id != ``', 0, y, h=10,
    desc="Start, completion and failure lines for the reconcile job, with duration, record "
         "counts and the JobExecution id."))

dashboard = {
    "uid": "webwork-integration",
    "title": "WebWork Integration Monitoring",
    "description": "Availability, traffic, latency, rate limiting, errors and jobs for the "
                   "WebWork integration. Metrics come from backend/utils/webworkMetrics.js and "
                   "logs from backend/utils/webworkLogger.js in the performance-tracker repo.",
    "tags": ["performance-tracker", "observability", "webwork", "integration"],
    "timezone": "browser",
    "editable": True,
    "schemaVersion": 39,
    "version": 1,
    "refresh": "1m",
    "time": {"from": "now-24h", "to": "now"},
    "panels": panels,
    "templating": {"list": [
        {
            # Only production is scraped today, so environment is not a metric label.
            # The Prometheus job is the closest real dimension: it says which process
            # made the call, api or cron, which is what you actually filter on.
            "name": "job", "label": "Process (env)", "type": "query", "datasource": PROM,
            "query": {"query": "label_values(webwork_requests_total, job)", "refId": "A"},
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]}, "refresh": 1,
        },
        {
            "name": "endpoint", "label": "Endpoint", "type": "query", "datasource": PROM,
            "query": {"query": "label_values(webwork_requests_total, endpoint)", "refId": "A"},
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]}, "refresh": 1,
        },
        {
            "name": "method", "label": "HTTP method", "type": "query", "datasource": PROM,
            "query": {"query": "label_values(webwork_requests_total, method)", "refId": "A"},
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]}, "refresh": 1,
        },
        {
            "name": "status", "label": "Status code", "type": "query", "datasource": PROM,
            "query": {"query": "label_values(webwork_requests_total, status)", "refId": "A"},
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]}, "refresh": 1,
        },
        {
            "name": "wwjob", "label": "Job", "type": "query", "datasource": PROM,
            "query": {"query": "label_values(webwork_job_runs_total, job_name)", "refId": "A"},
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]}, "refresh": 1,
        },
    ]},
    "annotations": {"list": []},
}

with open("grafana/dashboards/webwork-integration.json", "w") as f:
    json.dump(dashboard, f, indent=2)
    f.write("\n")

print("wrote grafana/dashboards/webwork-integration.json")
print("panels:", len([p for p in panels if p["type"] != "row"]),
      "+", len([p for p in panels if p["type"] == "row"]), "rows")
