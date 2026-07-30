import json

DS = {"type": "prometheus", "uid": "prometheus"}

panels = []
pid = 0


def nid():
    global pid
    pid += 1
    return pid


def targets(*specs):
    out = []
    for i, (expr, legend) in enumerate(specs):
        out.append({"datasource": DS, "expr": expr, "legendFormat": legend,
                    "refId": chr(65 + i), "editorMode": "code", "range": True})
    return out


def row(title, y):
    return {"type": "row", "title": title, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "id": nid(), "collapsed": False, "panels": []}


def stat(title, expr, x, y, w=6, h=4, unit="short", desc="", thresholds=None, legend=""):
    return {
        "type": "stat", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": DS,
        "targets": targets((expr, legend)),
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "area", "textMode": "auto",
                    "justifyMode": "auto", "orientation": "auto"},
        "fieldConfig": {"defaults": {"unit": unit, "mappings": [],
                                     "thresholds": thresholds or {"mode": "absolute",
                                                                  "steps": [{"color": "green", "value": None}]}},
                        "overrides": []},
    }


def ts(title, specs, x, y, w=12, h=8, unit="short", desc="", legend_calcs=None, minv=None, fill=10):
    return {
        "type": "timeseries", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": DS,
        "targets": targets(*specs),
        "options": {"legend": {"displayMode": "table" if legend_calcs else "list",
                               "placement": "bottom", "showLegend": True,
                               "calcs": legend_calcs or []},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "fieldConfig": {"defaults": {
            "unit": unit,
            "min": minv,
            "custom": {"drawStyle": "line", "lineWidth": 1, "fillOpacity": fill,
                       "showPoints": "never", "spanNulls": True,
                       "scaleDistribution": {"type": "linear"}},
            "color": {"mode": "palette-classic"},
            "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
        }, "overrides": []},
    }


DEDUP = ("max() because the gauge is exposed by both the api and cron jobs, which query the "
         "same Postgres; sum() would double it.")

# ── Health ───────────────────────────────────────────────────────
y = 0
panels.append(row("Health", y))
y += 1
panels.append(stat(
    "Targets up", "sum(up)", 0, y, w=6,
    desc="How many of the 3 jobs (api, cron, node) Prometheus can scrape.",
    thresholds={"mode": "absolute", "steps": [
        {"color": "red", "value": None}, {"color": "orange", "value": 2}, {"color": "green", "value": 3}]},
    legend="targets"))
panels.append(stat(
    "Queue: dead letter", "max(learnupon_queue_dead_letter)", 6, y, w=6,
    desc="Events that exhausted their retries. " + DEDUP,
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "red", "value": 1}]},
    legend="dead_letter"))
panels.append(stat(
    "Queue: failed", "max(learnupon_queue_failed)", 12, y, w=6,
    desc="Awaiting retry. " + DEDUP,
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "orange", "value": 20}]},
    legend="failed"))
panels.append(stat(
    "Queue: pending", "max(learnupon_queue_pending)", 18, y, w=6,
    desc="Received, not processed yet. " + DEDUP,
    legend="pending"))

# ── API HTTP ─────────────────────────────────────────────────────
y += 4
panels.append(row("API HTTP", y))
y += 1
panels.append(ts(
    "Requests/s by route",
    [("sum by (route) (rate(http_request_duration_seconds_count[5m]))", "{{route}}")],
    0, y, w=12, unit="reqps",
    desc="Rate per mounted route; concrete URLs never become labels. A request that "
         "matched no handler is route=\"unmatched\", and CORS preflight, which the cors "
         "middleware answers before routing, is route=\"cors-preflight\". Preflight used "
         "to land in unmatched and drown it: 697/s against 0.0/s of real 404s.",
    legend_calcs=["mean", "max"], minv=0))
panels.append(ts(
    "Latency p50 / p95 / p99",
    [("histogram_quantile(0.50, sum by (le) (rate(http_request_duration_seconds_bucket"
      "{route!=\"cors-preflight\"}[5m])))", "p50"),
     ("histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket"
      "{route!=\"cors-preflight\"}[5m])))", "p95"),
     ("histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket"
      "{route!=\"cors-preflight\"}[5m])))", "p99")],
    12, y, w=12, unit="s", minv=0,
    desc="Percentiles over the histogram buckets, every route that does work. CORS "
         "preflight is excluded: it is answered in microseconds and there is more of it "
         "than there is real traffic, so including it understated these. Measured at the "
         "time of the change, it pulled p95 from 7.4s down to 4.5s and p50 from 28ms to "
         "8ms, which reads as an API far faster than it is."))
y += 8
panels.append(ts(
    "5xx error rate",
    # `or vector(0)`: with no 5xx the numerator matches no series and the division
    # returns empty, painting "No data" — indistinguishable from a broken scrape,
    # in exactly the healthy case. The fallback renders zero errors as zero.
    [("(sum(rate(http_request_duration_seconds_count{status_code=~\"5..\"}[5m])) or vector(0)) "
      "/ sum(rate(http_request_duration_seconds_count{route!=\"cors-preflight\"}[5m]))", "5xx")],
    0, y, w=12, h=6, unit="percentunit",
    desc="Share of requests answered with 5xx. CORS preflight is out of the denominator: "
         "it never fails, so counting it dilutes the ratio by however much protocol "
         "overhead happens to be flowing. Phase 6 alert fires above 0.05.", minv=0))
panels.append(ts(
    "Requests/s by status",
    [("sum by (status_code) (rate(http_request_duration_seconds_count[5m]))", "{{status_code}}")],
    12, y, w=12, h=6, unit="reqps", minv=0,
    desc="Useful for spotting 4xx climbing before it turns into an incident."))

# ── Node processes ───────────────────────────────────────────────
y += 6
panels.append(row("Node processes (api and cron)", y))
y += 1
panels.append(ts(
    "Event loop lag p99",
    [("nodejs_eventloop_lag_p99_seconds", "{{job}}")],
    0, y, w=8, unit="s", minv=0,
    desc="Event loop delay per process. Phase 6 alert fires above 0.2."))
panels.append(ts(
    "Heap used",
    [("nodejs_heap_size_used_bytes", "{{job}}")],
    8, y, w=8, unit="bytes", minv=0,
    desc="V8 heap in use, per process."))
panels.append(ts(
    "Restarts per hour",
    [("changes(process_start_time_seconds{job=~\"api|cron\"}[1h])", "{{job}}")],
    16, y, w=8, unit="short",
    desc="process_start_time_seconds changes whenever the process restarts. Deploys count "
         "too (pm2 reload). Phase 6 alert fires above 3/h.",
    minv=0, fill=0))

# ── Database pool ─────────────────────────────
# Not deduplicated, unlike the gauges DEDUP describes: every process keeps its own
# pool, so the api workers and the cron are separate readings and collapsing them
# would hide the one that is saturated.
y += 8
panels.append(row("Database pool (per process)", y))
y += 1
panels.append(ts(
    "Waiting for a connection",
    [("db_pool_waiting", "{{job}} {{instance_id}}")],
    0, y, w=8, unit="short", minv=0,
    desc="Queries queued because every connection is checked out. Anything sustained above "
         "zero means the pool is the bottleneck, not the database and not CPU. On 2026-07-30 "
         "this shape produced 503s from the API Gateway while the app itself never returned "
         "a 5xx."))
panels.append(ts(
    "In use and available",
    [("db_pool_in_use", "in use {{job}} {{instance_id}}"),
     ("db_pool_available", "available {{job}} {{instance_id}}")],
    8, y, w=8, unit="short", minv=0,
    desc="Checked out against idle. Available at zero with in use at the ceiling is the "
         "saturated state; read it together with the waiting panel."))
panels.append(ts(
    "Saturation against the configured ceiling",
    [("100 * db_pool_in_use / db_pool_max", "{{job}} {{instance_id}}")],
    16, y, w=8, unit="percent", minv=0,
    desc="in use over db_pool_max, so the limit is not hardcoded here. The ceiling is per "
         "process, so two api workers at 100% is twice the connections of one."))

# ── LearnUpon ────────────────────────────────────────────────────
y += 8
panels.append(row("LearnUpon", y))
y += 1
panels.append(ts(
    "Queues over time",
    [("max(learnupon_queue_pending)", "pending"),
     ("max(learnupon_queue_failed)", "failed"),
     ("max(learnupon_queue_dead_letter)", "dead_letter")],
    0, y, w=12, unit="short",
    desc="max() deduplicates: the gauges arrive from both the api and cron jobs with the "
         "same value.",
    legend_calcs=["lastNotNull", "max"], minv=0))
panels.append(ts(
    "Webhook processing p95",
    [("histogram_quantile(0.95, sum by (le, event_type) "
      "(rate(learnupon_webhook_processing_seconds_bucket[5m])))", "{{event_type}}")],
    12, y, w=12, unit="s", minv=0,
    desc="Time spent dispatching an event to its handler, by type."))
y += 8
panels.append(ts(
    "Webhooks received/s by type",
    [("sum by (event_type) (rate(learnupon_webhooks_received_total[5m]))", "{{event_type}}")],
    0, y, w=12, unit="reqps", minv=0,
    desc="Emitted by the api job — webhooks come in through the API."))
panels.append(ts(
    "Events processed/s by status",
    [("sum by (status) (rate(learnupon_events_processed_total[5m]))", "{{status}}")],
    12, y, w=12, unit="reqps", minv=0,
    desc="Emitted by the cron job. status=failed climbing precedes dead letter growth."))

# ── Logs ─────────────────────────────────────────────────────────
y += 8
panels.append(row("Logs", y))
y += 1
panels.append({
    "type": "logs", "title": "Errors and warnings", "id": nid(),
    "description": "Error and warning lines from the API, read from the level field. "
                   "filename tells the two clustered instances apart (server-out-8 vs "
                   "server-out-9). Lines that fail to parse are kept: those are the "
                   "crashes that never reach the logger, and dropping them would hide "
                   "exactly the worst failures.",
    "gridPos": {"h": 12, "w": 24, "x": 0, "y": y},
    "datasource": {"type": "loki", "uid": "loki"},
    # One query now. This used to be two, because the backend logged plain text
    # and the only way to find a failure was to read all of stderr and then
    # grep stdout for the words error and warn. Both premises are gone: pino
    # writes structured JSON, and it writes every level to stdout, so stderr
    # carries no application errors at all. Reading the level field replaces
    # both queries.
    #
    # The or-clause keeps lines that fail to parse. Those are Node's own
    # crashes and PM2's notices, which never go through the logger, and they
    # are the failures worth seeing most.
    "targets": [
        {"datasource": {"type": "loki", "uid": "loki"},
         "expr": '{job="server"} | json '
                 '| level=~"error|warn" or __error__="JSONParserErr"',
         "refId": "A", "queryType": "range"},
    ],
    "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                "enableLogDetails": True, "dedupStrategy": "none", "prettifyLogMessage": False},
})

dashboard = {
    "uid": "pt-api",
    "title": "Performance Tracker API",
    "description": "Application metrics for the Performance Tracker (api and cron jobs). "
                   "EC2 host metrics live in the Node Exporter Full dashboard.",
    "tags": ["performance-tracker", "observability"],
    "timezone": "browser",
    "editable": True,
    "schemaVersion": 39,
    "version": 1,
    "refresh": "30s",
    "time": {"from": "now-6h", "to": "now"},
    "panels": panels,
    "templating": {"list": []},
    "annotations": {"list": []},
}

with open("grafana/dashboards/performance-tracker-api.json", "w") as f:
    json.dump(dashboard, f, indent=2)
    f.write("\n")

print("wrote grafana/dashboards/performance-tracker-api.json")
print("panels:", len([p for p in panels if p["type"] != "row"]),
      "+", len([p for p in panels if p["type"] == "row"]), "rows")
