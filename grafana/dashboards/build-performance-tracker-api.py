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


def targets(*specs, instant=False):
    # instant=False (every existing caller) keeps the exact prior shape: range
    # only, no "instant" key. instant=True is for a stat that reduces to one
    # point anyway, so Grafana evaluates the query once instead of at every
    # step across the dashboard's time range.
    out = []
    for i, (expr, legend) in enumerate(specs):
        t = {"datasource": DS, "expr": expr, "legendFormat": legend,
             "refId": chr(65 + i), "editorMode": "code", "range": not instant}
        if instant:
            t["instant"] = True
        out.append(t)
    return out


def row(title, y):
    return {"type": "row", "title": title, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "id": nid(), "collapsed": False, "panels": []}


def stat(title, expr, x, y, w=6, h=4, unit="short", desc="", thresholds=None, legend="",
          instant=False):
    return {
        "type": "stat", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": DS,
        "targets": targets((expr, legend), instant=instant),
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "area", "textMode": "auto",
                    "justifyMode": "auto", "orientation": "auto"},
        "fieldConfig": {"defaults": {"unit": unit, "mappings": [],
                                     "thresholds": thresholds or {"mode": "absolute",
                                                                  "steps": [{"color": "green", "value": None}]}},
                        "overrides": []},
    }


def ts(title, specs, x, y, w=12, h=8, unit="short", desc="", legend_calcs=None, minv=None, fill=10,
       threshold=None, overrides=None, span_nulls=True):
    # threshold draws a dashed reference line at that value, e.g. the 60s latency
    # promise. overrides carries per-field fieldConfig overrides (see `override()`),
    # for the rare panel that needs a series on its own axis or unit. span_nulls
    # defaults True to match every existing panel; set False for a low-volume
    # series where a quiet period must show as a gap, not a fabricated straight
    # line that can sit on either side of a threshold.
    custom = {"drawStyle": "line", "lineWidth": 1, "fillOpacity": fill,
              "showPoints": "never", "spanNulls": span_nulls,
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
# false here. sum_over_time() is also wrong, and not for the same reason it is
# wrong on a real counter: this Prometheus scrapes every 15s while the poller
# only writes every 300s, so sum_over_time repeats and adds each five-minute
# value roughly 20 times over a range, turning 3 real errors into 60. The two
# range-total panels below use max_over_time() instead, which reads the same
# repeated samples but takes their peak, answering "did this happen" correctly
# regardless of scrape rate; the two plain timeseries panels below plot the
# gauge directly with no range function at all, since a graph over time needs
# no reduction to a single number. Every one of these gauges holds its last
# value if the poller stops, so a flat reading can mean a quiet Lambda or a
# stalled poller, and no panel here can tell those apart alone; each panel's
# own description says so, since a reader looking at one panel does not see
# this comment.
#
# No template variables are applied to this section: this dashboard's
# templating list is empty, and the only label the CloudWatch-bridged metrics
# carry is environment, which none of this board's variables bind to anyway.
#
# Three panels here aggregate with max() where the rest of this file (and the
# counters right next to them, dropped/quarantined) use sum(): clock_events_
# pending, aws_lambda_errors and aws_lambda_throttles each mirror one upstream
# number (a single DynamoDB count, CloudWatch's own per-function figures)
# rather than partitioning work across instances, so summing what a second
# process publishing the same reading would double-count is wrong, the same
# reasoning DEDUP documents above for the LearnUpon queue gauges. This also
# is not optional once `or vector(0)` is involved: `or` matches on the full
# label set, vector(0) has none, and a real series here still carries job and
# instance, which are never equal to that empty set, so a bare `metric or
# vector(0)` unions a permanent phantom zero in on every healthy render
# instead of ever replacing anything. max()/sum() strip labels to `{}` first,
# which is what lets the fallback actually take over when the series is
# absent.
y += 8
panels.append(row("Clock service", y))
y += 1

# Step 1: immediate health. Four stats that should read zero.
panels.append(stat(
    "Events pending", "max(clock_events_pending) or vector(0)", 0, y, w=6,
    desc="Events queued by the clock service waiting to drain into time_tracker. Published "
         "directly by the clock cron jobs as a real-time gauge, not bridged from CloudWatch, "
         "so this reflects the current depth rather than a five-minute-old sample. Wrapped "
         "in max(), not sum(): this gauge mirrors one upstream DynamoDB count rather than "
         "partitioning work, so a second process reporting the same reading must not be "
         "added to itself, and only a labelless aggregate lets the `or vector(0)` fallback "
         "actually replace a missing series instead of being unioned in beside it as a "
         "second, permanently-zero tile (a real series here still carries job/instance, "
         "which never equals vector(0)'s empty label set). Red above 50 to match the "
         "Task 7 alert rule; normal operation reads at or near zero.",
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "red", "value": 50}]},
    legend="pending"))
panels.append(stat(
    "Events dropped",
    "clamp_min((sum(clock_events_dropped_total) or vector(0)) - "
    "(sum(clock_events_dropped_total offset $__range) or vector(0)), 0)",
    6, y, w=6, instant=True,
    desc="Events the clock service discarded rather than delivered, over the selected range. "
         "summed across every reason into one total rather than increase(): "
         "clock_events_dropped_total is a labelled counter, so a reason's first-ever drop "
         "creates that child mid-window, every sample of it then holds the same value, and "
         "increase() reports 0 for that case (Prometheus's zero-extrapolation only fires when "
         "the raw delta is already positive), rendering green at the exact moment it should "
         "fire red. This range-offset difference does not have that blind spot, but it also "
         "does not detect a genuine counter reset the way increase() does: a cron restart "
         "inside the window can make this figure undercount. Both terms carry `or vector(0)` "
         "so a metric with no data yet subtracts as zero instead of producing No data, and "
         "clamp_min floors the result at 0 since this form has no reset handling of its own. "
         "Should read zero; Task 7's alert on this same metric is the authority, not this "
         "panel.",
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "red", "value": 1}]},
    legend="dropped"))
panels.append(stat(
    "Events quarantined",
    "clamp_min((sum(clock_events_quarantined_total) or vector(0)) - "
    "(sum(clock_events_quarantined_total offset $__range) or vector(0)), 0)",
    12, y, w=6, instant=True,
    desc="Events set aside for manual review rather than dropped or drained, over the "
         "selected range. Same range-offset difference as Events dropped and for the same "
         "reason: increase() reports 0 for the very first occurrence inside the window, "
         "because a newly created counter child holds a constant value across every sample "
         "until the next one. clamp_min floors the result at 0, since this form, unlike "
         "increase(), does not detect a genuine counter reset; a cron restart inside the "
         "window can still make this figure undercount. Should read zero; Task 7's alert on "
         "this same metric is the authority for catching the very first occurrence "
         "reliably, not this panel.",
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "red", "value": 1}]},
    legend="quarantined"))
panels.append(stat(
    "Lambda errors (worst 5-minute window)",
    "max(max_over_time(aws_lambda_errors[$__range])) or vector(0)", 18, y, w=6, instant=True,
    desc="The single worst five-minute window's Lambda invocation errors inside the selected "
         "range, not a running total. aws_lambda_errors is a gauge that a CloudWatch poller "
         "overwrites every five minutes with that window's own Sum; Prometheus itself scrapes "
         "far more often (every 15s), so summing the raw samples with sum_over_time would "
         "count each five-minute value roughly 20 times over, turning 3 real errors into 60. "
         "max_over_time takes the peak of those repeated samples instead, which answers "
         "\"did this happen\" correctly regardless of scrape rate or a gap in polling. The "
         "outer max() is for the same reason as Events pending above: this is CloudWatch's "
         "own single number for the function, not a per-instance count, so max() rather than "
         "sum() avoids double-counting and is also what lets `or vector(0)` replace a "
         "missing series instead of adding a permanent phantom zero beside it. The value "
         "refreshes every five minutes and holds its last reading if the poller stops, so a "
         "flat zero can also mean a dead poller rather than a healthy Lambda. Should read "
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
    0, y, w=24, unit="s", minv=0, threshold=60, span_nulls=False,
    desc="The design promises a punch reaches time_tracker within a minute; this is that "
         "promise, watched continuously rather than audited by hand. The dashed line at 60 "
         "seconds is that one-minute promise made visible on the graph: p95 crossing it means "
         "the promise is being broken for at least 1 in 20 punches, not merely that something "
         "is slower than usual. Gaps are left open rather than interpolated across: punch "
         "volume is low enough that a quiet stretch makes every bucket's rate 0 and "
         "histogram_quantile returns no series at all, and spanning that gap would draw a "
         "straight fabricated line that could sit on either side of the 60s promise this "
         "panel exists to judge."))
y += 8

# Step 3: the migration view.
panels.append(ts(
    "Migration progress: clock service vs portal punches",
    [("sum by (company_slug) (rate(clock_events_drained_total[$__rate_interval]))", "Clock: {{company_slug}}"),
     ("(sum(rate(webwork_requests_total{endpoint=~\"/time-tracking/(start|stop)\"}"
      "[$__rate_interval])) or vector(0))", "Portal punches")],
    0, y, w=24, unit="reqps", minv=0,
    desc="Punches drained through the clock service, by brand, next to the portal's own "
         "/time-tracking/start and /stop calls on the same axis: this is the curve that says "
         "when Reach can be enabled and when the portal's own start and stop endpoints can be "
         "deleted. Legends are prefixed (\"Clock: teem\") because an unlabelled brand name "
         "next to \"Portal punches\" reads as the portal's own traffic for that brand, which "
         "inverts the whole point of a migration panel. webwork_requests_total is published "
         "by the portal only; the Lambda's WebWork calls never pass through it, so these two "
         "lines are disjoint populations, not a double count of the same punches. Portal "
         "punches is wrapped in `or vector(0)`: once those endpoints stop being called and "
         "the API process next restarts, prom-client will not recreate the series, and "
         "without this fallback the line would vanish from the graph entirely instead of "
         "reading as zero. As the migration completes, Portal punches is expected to fall to "
         "(and stay at) zero while the by-brand clock lines carry everything; a Portal "
         "punches line flatlined at zero later is the success condition this panel was built "
         "to show, not a sign the panel is broken."))
y += 8

# Step 4: Lambda infrastructure.
panels.append(ts(
    "Lambda invocations, concurrency and duration",
    [("aws_lambda_invocations", "{{environment}} invocations"),
     ("aws_lambda_concurrent_executions", "{{environment}} concurrent executions"),
     ("aws_lambda_duration_seconds", "{{environment}} duration (s)")],
    0, y, w=18, unit="short", minv=0,
    overrides=[override("byRegexp", ".*duration.*",
                         [{"id": "unit", "value": "s"},
                          {"id": "custom.axisPlacement", "value": "right"}])],
    desc="Invocations and concurrent executions (left axis, both counts) and duration (right "
         "axis, seconds) for the clock Lambda, sharing one panel because a count and a "
         "duration cannot share a meaningful scale on their own axis. Concurrent executions "
         "sits beside invocations rather than off on its own: it is the number that predicts "
         "a throttle, since concurrency climbing toward the account's reserved-concurrency "
         "ceiling is what causes the throttles stat beside this panel to move, so a reader "
         "chasing a throttle spike wants this line on the same read, not a separate panel. "
         "All three series are gauges bridged from CloudWatch, each overwritten every five "
         "minutes by the poller and each already holding CloudWatch's own aggregate for that "
         "window rather than a raw sample (invocations Sum, duration Average; concurrent "
         "executions is a live count rather than something summed or averaged over the "
         "window), so all three are plotted directly with no rate() or increase() because a "
         "gauge is not a counter. Each holds its last reading if the poller stops, so a flat "
         "line here is consistent with either quiet traffic or a stalled poller; this panel "
         "alone cannot tell the two apart. Metered on every poll regardless of whether "
         "anything changed, at roughly nine cents a month of the poller's total cost; this "
         "panel is the only reader of it, so that cost now buys something."))
panels.append(stat(
    "Lambda throttles (worst 5-minute window)",
    "max(max_over_time(aws_lambda_throttles[$__range])) or vector(0)", 18, y, w=6, h=8, instant=True,
    desc="The single worst five-minute window's Lambda throttles inside the selected range, "
         "not a running total. aws_lambda_throttles is a gauge that a CloudWatch poller "
         "overwrites every five minutes with that window's own Sum; Prometheus itself scrapes "
         "far more often (every 15s), so summing the raw samples with sum_over_time would "
         "count each five-minute value roughly 20 times over. max_over_time takes the peak of "
         "those repeated samples instead, which answers \"did this happen\" correctly "
         "regardless of scrape rate or a gap in polling. The outer max(), same as the other "
         "two CloudWatch-bridged stats in this row, is because this is CloudWatch's own "
         "single number rather than a per-instance count: max() avoids double-counting if a "
         "second poller ever existed, and it is also what lets `or vector(0)` replace a "
         "missing series instead of adding a permanent phantom zero beside it. The value "
         "refreshes every five minutes and holds its last reading if the poller stops, so a "
         "flat zero can also mean a dead poller rather than a healthy Lambda. Any value "
         "above zero means the Lambda hit its concurrency ceiling and a punch was delayed or "
         "rejected.",
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "red", "value": 1}]},
    legend="throttles"))
y += 8

# Step 5: context sync. Half this service and, until now, on no panel at
# all: the only place it showed up was a status cell on the WebWork board.
panels.append(ts(
    "Context sync: items and writes",
    [("clock_context_items", "{{company_slug}} items"),
     ("rate(clock_context_items_written_total[$__rate_interval])", "written/s")],
    0, y, w=12, unit="short", minv=0,
    overrides=[override("byName", "written/s",
                         [{"id": "unit", "value": "wps"},
                          {"id": "custom.axisPlacement", "value": "right"}])],
    desc="Whether the context sync is fresh and actually writing. clock_context_items (left "
         "axis) is the live item count per brand, held by the clock-context-sync cron job. "
         "clock_context_items_written_total (right axis, writes/second) is how many of them "
         "changed and got written in this window. written/s reading zero is the normal "
         "steady state, not a failure: the sync only writes what changed since its last run, "
         "so a quiet period with nothing new produces zero writes by design, not an outage."))

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
