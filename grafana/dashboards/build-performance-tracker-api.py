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
    desc="Rate per mounted route; concrete URLs never become labels. Unmatched requests "
         "collapse into route=\"unmatched\".",
    legend_calcs=["mean", "max"], minv=0))
panels.append(ts(
    "Latency p50 / p95 / p99",
    [("histogram_quantile(0.50, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))", "p50"),
     ("histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))", "p95"),
     ("histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket[5m])))", "p99")],
    12, y, w=12, unit="s", minv=0,
    desc="Percentiles over the histogram buckets, all routes combined."))
y += 8
panels.append(ts(
    "5xx error rate",
    # `or vector(0)`: with no 5xx the numerator matches no series and the division
    # returns empty, painting "No data" — indistinguishable from a broken scrape,
    # in exactly the healthy case. The fallback renders zero errors as zero.
    [("(sum(rate(http_request_duration_seconds_count{status_code=~\"5..\"}[5m])) or vector(0)) "
      "/ sum(rate(http_request_duration_seconds_count[5m]))", "5xx")],
    0, y, w=12, h=6, unit="percentunit",
    desc="Share of requests answered with 5xx. Phase 6 alert fires above 0.05.", minv=0))
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
    "description": "All of stderr, plus anything on stdout matching error/warn. Level filtering "
                   "is heuristic while the backend still logs through console.* — see "
                   "docs/superpowers/specs/2026-07-28-loki-logs-design.md. filename tells the two "
                   "clustered API instances apart (server-out-8 vs server-out-9).",
    "gridPos": {"h": 12, "w": 24, "x": 0, "y": y},
    "datasource": {"type": "loki", "uid": "loki"},
    # Two queries, not one. LogQL cannot `or` stream selectors together:
    # `{stream="err"} or {stream="out"} |~ ...` is a parse error. The logs panel
    # accepts several targets and merges them for display.
    #   A: all of stderr, whether or not the text says "error" — the real
    #      failures observed look like `[user-access] ... cron failed:`
    #   B: only the stdout lines matching error/warn, else the whole
    #      informational stream would drown the panel
    "targets": [
        {"datasource": {"type": "loki", "uid": "loki"},
         "expr": '{stream="err"}', "refId": "A", "queryType": "range"},
        {"datasource": {"type": "loki", "uid": "loki"},
         "expr": '{stream="out"} |~ "(?i)(error|warn)"', "refId": "B", "queryType": "range"},
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
