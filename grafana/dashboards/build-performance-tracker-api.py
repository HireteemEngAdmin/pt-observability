import json

DS = {"type": "prometheus", "uid": "prometheus"}

# Per-user work has no Prometheus dimension and is not going to get one: the http
# histogram is labelled method/route/status_code on purpose, and a user id is
# unbounded cardinality. Loki carries userId and tenantId on every request line,
# so the "who" panels below read from there.
LOKI = {"type": "loki", "uid": "loki"}

# What the request logger writes for a response it refused. `request rate limited`
# is deliberately NOT here: that line is written by the limiter itself and carries
# no user ("the address is deliberately absent"), so summing by userId over it
# returns one anonymous bucket. requestLogger logs the same requests anyway, 429
# included, and those lines do carry the user.
REFUSED = r'| json | msg=~"request rejected|request failed"'

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


def ts(title, specs, x, y, w=12, h=8, unit="short", desc="", legend_calcs=None, minv=None, fill=10,
       threshold=None, overrides=None):
    # threshold draws a dashed reference line at that value, e.g. the 60s latency
    # promise. overrides carries per-field fieldConfig overrides (see `override()`),
    # for the rare panel that needs a series on its own axis or unit.
    custom = {"drawStyle": "line", "lineWidth": 1, "fillOpacity": fill,
              "showPoints": "never", "spanNulls": True,
              "scaleDistribution": {"type": "linear"}}
    thresholds = {"mode": "absolute", "steps": [{"color": "green", "value": None}]}
    if threshold is not None:
        custom["thresholdsStyle"] = {"mode": "dashed"}
        thresholds = {"mode": "absolute", "steps": [{"color": "green", "value": None},
                                                     {"color": "red", "value": threshold}]}
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
            "custom": custom,
            "color": {"mode": "palette-classic"},
            "thresholds": thresholds,
        }, "overrides": overrides or []},
    }


def override(matcher_id, options, props):
    return {"matcher": {"id": matcher_id, "options": options}, "properties": props}


def loki_targets(*exprs, instant=False):
    return [{"datasource": LOKI, "expr": e, "refId": chr(65 + i),
             "queryType": "instant" if instant else "range", "editorMode": "code"}
            for i, e in enumerate(exprs)]


def loki_stat(title, expr, x, y, w=8, h=4, desc=""):
    return {
        "type": "stat", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": LOKI,
        "targets": loki_targets(expr, instant=True),
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "none", "textMode": "auto",
                    "justifyMode": "auto", "orientation": "auto"},
        "fieldConfig": {"defaults": {"unit": "short", "mappings": [],
                                     "thresholds": {"mode": "absolute",
                                                    "steps": [{"color": "text", "value": None}]}},
                        "overrides": []},
    }


def loki_bar(title, expr, x, y, w=12, h=10, desc=""):
    """Top-N over the window as a bar gauge.

    instant, not range: a range query evaluates the topk at every step, so the
    panel receives N series times ~45 steps and draws unreadable slivers instead
    of N bars. Top-N over a window is a single evaluation.

    Loki answers with one frame per series and calls the numeric field "Value" in
    all of them; the label exists only in the frame's labels. labelsToFields
    promotes it, merge folds the frames into one table, and values:true then draws
    one bar per row. A displayName template cannot do this - it is resolved before
    the frames are merged.
    """
    return {
        "type": "bargauge", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": LOKI,
        "targets": loki_targets(expr, instant=True),
        "transformations": [
            {"id": "labelsToFields", "options": {}},
            {"id": "merge", "options": {}},
            {"id": "sortBy", "options": {"sort": [{"field": "Value #A", "desc": True}]}},
            {"id": "organize", "options": {"excludeByName": {"Time": True}}},
        ],
        "options": {"orientation": "horizontal", "displayMode": "gradient",
                    "showUnfilled": True, "valueMode": "text", "minVizWidth": 8,
                    "minVizHeight": 16, "namePlacement": "left", "sizing": "auto",
                    "reduceOptions": {"calcs": [], "fields": "", "values": True}},
        "fieldConfig": {"defaults": {
            "unit": "short", "min": 0,
            "color": {"mode": "continuous-BlPu"},
            "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
        }, "overrides": []},
    }


def loki_ts(title, specs, x, y, w=12, h=8, unit="short", desc=""):
    panel = ts(title, specs, x, y, w=w, h=h, unit=unit, desc=desc, minv=0)
    panel["datasource"] = LOKI
    panel["targets"] = [{"datasource": LOKI, "expr": e, "legendFormat": lf,
                         "refId": chr(65 + i), "queryType": "range", "editorMode": "code"}
                        for i, (e, lf) in enumerate(specs)]
    return panel


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
    "Latency p99 by route (top 5)",
    [("topk(5, histogram_quantile(0.99, sum by (le, route) (rate("
      "http_request_duration_seconds_bucket{route!=\"cors-preflight\"}[5m]))))", "{{route}}")],
    0, y, w=12, unit="s", minv=0,
    desc="The panel above says something is slow; this one says what. Read it with the "
         "one beside it, never alone: a route with a handful of requests can post a "
         "dramatic p99 off a single slow call and outrank a route that is actually "
         "shaping the aggregate. Latency here, weight there."))
panels.append(ts(
    "Requests slower than 0.5s, by route (top 5)",
    [("topk(5, sum by (route) (rate(http_request_duration_seconds_count"
      "{route!=\"cors-preflight\"}[5m])) - sum by (route) (rate("
      "http_request_duration_seconds_bucket{route!=\"cors-preflight\",le=\"0.5\"}[5m])))", "{{route}}")],
    12, y, w=12, unit="reqps", minv=0,
    desc="Who actually draws the aggregate p99. The p99 line is the slowest 1% of "
         "requests, so the route supplying most of them sets it, however healthy "
         "everything else is. Measured over one 6h window: 98.6% of requests finished "
         "under 0.5s, and of the 1,588 that did not, /api/time-tracker/init supplied "
         "68.5%, which was the whole of the p99 people were asking about. "
         "Its median equals the mean WebWork call it waits on, 0.54s against 0.539s, so "
         "the number was never about the database or the pool. "
         "Note the bucket label is le=\"0.5\": these carry a decimal, and le=\"0.50\" "
         "matches nothing and paints an empty panel that reads as good news."))
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

# ── Who the API is refusing ──────────────────────────────────────
#
# READ THE TITLES LITERALLY. These are refusals, not traffic.
#
# A successful request is logged at debug and production does not emit debug, so
# `request completed` reaches Loki zero times. Measured over 6h: 43,886 lines of
# `request rejected` and not one of `request completed`. There is no per-user view
# of ordinary traffic anywhere in this stack, and these panels are not it. They
# answer "who is the API saying no to", which is the question an incident asks.
#
# The user column is a uuid because the request line carries no name. Resolving it
# needs the database; the dashboard cannot.
y += 6
panels.append(row("Who the API is refusing", y))
y += 1
panels.append(loki_stat(
    "Users refused", 'count(count by (userId) (count_over_time({job="server"} '
    + REFUSED + ' | userId != "" [$__range])))', 0, y, w=8,
    desc="Distinct signed-in users that got a 4xx or 5xx in this window. The number is "
         "the shape of the problem: two is somebody's bad afternoon, and 186 was the "
         "render loop of 2026-08-11. Unauthenticated requests are excluded here, so this "
         "can read one lower than the bar gauge below, which does show them."))
panels.append(loki_stat(
    "Tenants refused", 'count(count by (tenantId) (count_over_time({job="server"} '
    + REFUSED + ' | tenantId != "" [$__range])))', 8, y, w=8,
    desc="Distinct tenants behind those users. One tenant with many users is a client "
         "whose whole office is affected; many tenants is ours."))
panels.append(loki_stat(
    "Refusals", 'sum(count_over_time({job="server"} ' + REFUSED + ' [$__range]))',
    16, y, w=8,
    desc="Every 4xx and 5xx the API answered in this window, including the 429s the "
         "rate limiter produced."))
y += 4
panels.append(loki_bar(
    "Users refused most (top 10)",
    'label_replace(topk(10, sum by (userId) (count_over_time({job="server"} '
    + REFUSED + ' [$__range]))), "userId", "(not signed in)", "userId", "^$")',
    0, y, w=12,
    desc="One bar per user id. The empty bucket is renamed rather than hidden: requests "
         "with no user are unauthenticated traffic, which is worth seeing next to the "
         "rest instead of silently dropped. "
         "Concentration is the signal here, not the total. On 2026-08-11 four people out "
         "of 139 supplied 55% of a million refusals, and every one of them was a browser "
         "tab in a render loop rather than a person doing anything."))
panels.append(loki_bar(
    "Tenants refused most (top 10)",
    'label_replace(topk(10, sum by (tenantId) (count_over_time({job="server"} '
    + REFUSED + ' [$__range]))), "tenantId", "(no tenant)", "tenantId", "^$")',
    12, y, w=12,
    desc="The same window grouped by tenant. Read it against the panel beside it: one "
         "tenant made of one user is a single stuck client, and one tenant made of many "
         "is something that reached the whole office."))
y += 10
panels.append(loki_bar(
    "Paths refused most (top 10)",
    'label_replace(topk(10, sum by (path) (count_over_time({job="server"} '
    + REFUSED + ' [$__range]))), "path", "(a handler matched, see route)", "path", "^$")',
    0, y, w=12,
    desc="path is the normalised url, ids collapsed to :id and the query string dropped. "
         "It is recorded only when no handler matched, which is the case for everything "
         "the rate limiter refuses, so this is the panel that separates "
         "/api/time-tracker/init from /api/time-tracker/start - one label upstairs, two "
         "very different problems. A request that did reach a handler has no path and is "
         "named by the bucket instead."))
panels.append(loki_ts(
    "Refusals by status over time",
    [('sum by (statusCode) (count_over_time({job="server"} ' + REFUSED
      + ' [$__interval]))', "{{statusCode}}")],
    12, y, w=12,
    desc="A range query rather than a top-N: statusCode is a closed set, so it cannot "
         "blow up the way a user or path grouping would. Each point is a count inside "
         "one interval bucket, not a rate, which is how the logs board draws the same "
         "shape. 429 climbing on its own is the rate limiter doing its job against a "
         "client that will not stop; 401 and 403 climbing together is usually a session "
         "or company-scope problem."))

# ── Node processes ───────────────────────────────────────────────
y += 10
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

# ── Clock service ────────────────────────────────────────────────
#
# On this board, not a separate one, because correlation is the point: portal
# saturation and clock latency need to sit on one time axis, and a separate
# board would recreate the problem this row solves.
#
# Five of these metrics (aws_lambda_*) are bridged from CloudWatch by a poller
# that writes once every five minutes; each is a gauge already holding
# CloudWatch's own Sum or Average for that trailing window, not a counter that
# climbs forever. rate() and increase() assume a monotonic counter, which is
# false here, so ranges are totalled with sum_over_time() instead: it adds up
# the completed five-minute sums the gauge already contains rather than
# computing a rate off a series that jumps up and down. Every one of these
# gauges holds its last value if the poller stops, so a flat reading can mean
# a quiet Lambda or a stalled poller, and no panel here can tell those apart
# alone.
#
# No template variables are applied to this section: this dashboard's
# templating list is empty, and the only label the CloudWatch-bridged metrics
# carry is environment, which none of this board's variables bind to anyway.
y += 8
panels.append(row("Clock service", y))
y += 1

# Step 1: immediate health. Four stats that should read zero.
panels.append(stat(
    "Events pending", "clock_events_pending", 0, y, w=6,
    desc="Events queued by the clock service waiting to drain into time_tracker. Published "
         "directly by the clock cron jobs as a real-time gauge, not bridged from CloudWatch, "
         "so this reflects the current depth rather than a five-minute-old sample. Red above "
         "50 to match the Task 7 alert rule; normal operation reads at or near zero.",
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "red", "value": 50}]},
    legend="pending"))
panels.append(stat(
    "Events dropped", "increase(clock_events_dropped_total[$__range])", 6, y, w=6,
    desc="Events the clock service discarded rather than delivered, over the selected range. "
         "Carries a reason label upstream, so more than one reason firing in the window shows "
         "as more than one number in this panel rather than one blended total. Should read "
         "zero; any value above it is a punch that never reached time_tracker.",
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "red", "value": 1}]},
    legend="dropped"))
panels.append(stat(
    "Events quarantined", "increase(clock_events_quarantined_total[$__range])", 12, y, w=6,
    desc="Events set aside for manual review rather than dropped or drained, over the "
         "selected range. Should read zero; any value above it means an event needs a human "
         "before it can be processed.",
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "red", "value": 1}]},
    legend="quarantined"))
panels.append(stat(
    "Lambda errors", "sum_over_time(aws_lambda_errors[$__range])", 18, y, w=6,
    desc="Lambda invocation errors, totalled over the selected range. aws_lambda_errors is a "
         "CloudWatch-bridged gauge (see the section note above): sum_over_time adds up the "
         "completed five-minute Sums it already holds across the range, which is the form "
         "that matches what this series actually contains; increase() or rate() would "
         "silently misread it, because the value is not a monotonic counter. Should read "
         "zero.",
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "red", "value": 1}]},
    legend="errors"))
y += 4

# Step 2: the latency the design promises.
panels.append(ts(
    "Punch-to-time_tracker latency (p50 / p95)",
    [("histogram_quantile(0.50, sum by (le) (rate(clock_event_latency_seconds_bucket"
      "[$__rate_interval])))", "p50"),
     ("histogram_quantile(0.95, sum by (le) (rate(clock_event_latency_seconds_bucket"
      "[$__rate_interval])))", "p95")],
    0, y, w=24, unit="s", minv=0, threshold=60,
    desc="The design promises a punch reaches time_tracker within a minute; this is that "
         "promise, watched continuously rather than audited by hand. The dashed line at 60 "
         "seconds is that one-minute promise made visible on the graph: p95 crossing it means "
         "the promise is being broken for at least 1 in 20 punches, not merely that something "
         "is slower than usual."))
y += 8

# Step 3: the migration view.
panels.append(ts(
    "Migration progress: clock service vs portal punches",
    [("sum by (company_slug) (rate(clock_events_drained_total[$__rate_interval]))", "{{company_slug}}"),
     ("sum(rate(webwork_requests_total{endpoint=~\"/time-tracking/(start|stop)\"}"
      "[$__rate_interval]))", "Portal punches")],
    0, y, w=24, unit="reqps", minv=0,
    desc="Punches drained through the clock service, by brand, next to the portal's own "
         "/time-tracking/start and /stop calls on the same axis: this is the curve that says "
         "when Reach can be enabled and when the portal's own start and stop endpoints can be "
         "deleted. webwork_requests_total is published by the portal only; the Lambda's "
         "WebWork calls never pass through it, so these two lines are disjoint populations, "
         "not a double count of the same punches. As the migration completes, Portal punches "
         "is expected to fall to zero while the by-brand clock lines carry everything; a "
         "Portal punches line flatlined at zero later is the success condition this panel was "
         "built to show, not a sign the panel is broken."))
y += 8

# Step 4: Lambda infrastructure.
panels.append(ts(
    "Lambda invocations and duration",
    [("aws_lambda_invocations", "{{environment}} invocations"),
     ("aws_lambda_duration_seconds", "{{environment}} duration (s)")],
    0, y, w=18, unit="short", minv=0,
    overrides=[override("byRegexp", ".*duration.*",
                         [{"id": "unit", "value": "s"},
                          {"id": "custom.axisPlacement", "value": "right"}])],
    desc="Invocations (left axis, a count) and duration (right axis, seconds) for the clock "
         "Lambda, sharing one panel because a count and a duration cannot share a meaningful "
         "scale on their own axis. Both series are CloudWatch-bridged gauges (see the section "
         "note above): invocations already holds CloudWatch's Sum of calls for the trailing "
         "five-minute window, duration already holds its Average, and both are plotted "
         "directly rather than through rate() or increase() because a gauge is not a counter. "
         "A flat line here is consistent with either quiet traffic or a stalled poller; this "
         "panel alone cannot tell the two apart."))
panels.append(stat(
    "Lambda throttles (range)", "sum_over_time(aws_lambda_throttles[$__range])", 18, y, w=6, h=8,
    desc="Lambda invocations throttled by concurrency limits, totalled over the selected "
         "range. Same gauge shape as the health stats above: aws_lambda_throttles holds "
         "CloudWatch's own five-minute Sum, so sum_over_time adds up the completed window "
         "sums instead of increase() or rate() misreading a series that is not a monotonic "
         "counter. Any value above zero means the Lambda hit its concurrency ceiling and a "
         "punch was delayed or rejected.",
    legend="throttles"))

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
