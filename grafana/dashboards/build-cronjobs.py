"""Generate the Cronjobs Monitoring dashboard.

Run from the repo root:  python3 grafana/dashboards/build-cronjobs.py

Backed by the thirteen metrics in performance-tracker
backend/utils/cronMetrics.js and the four pino events emitted by
backend/utils/instrumentCronJob.js, which reach Loki under job="cron".

Four things this file gets right that are easy to get wrong, all of them
verified against a Prometheus rather than reasoned about:

1. The application instance is `instance_id`, not `instance`. Prometheus
   attaches its own `instance` label naming the scrape target, so a metric
   declaring that name collides with it: with honor_labels off the scraped one
   is renamed to `exported_instance`, and a query for `instance="0"` matches
   nothing, silently. cronjob_running first declared `instance` and was fixed at
   the source to declare `instance_id` instead, so the label survives the scrape
   and no dashboard has to say `exported_instance` forever. Verified by scraping
   both versions of the exposition into a throwaway Prometheus.

2. `trigger` is not a Prometheus label at all. instrumentCronJob puts it in the
   childLogger bindings, not in any metric's labelNames. So the trigger variable
   can only bind to the Loki panels, and it is a custom variable rather than a
   query one because there is no label to enumerate.

3. The two systems disagree about process names, but not for cron. Prometheus
   scrapes jobs named `api` and `cron`; Alloy names Loki streams after the PM2
   process, which are `server` and `cron`. The API process is therefore `api`
   in one and `server` in the other, while the cron process is `cron` in both.
   Every metric here comes from the cron process only, so the Loki panels pin
   `{job="cron"}` literally. Checked against Loki's live label values.

4. Counters are always read through rate() or increase(). The cron process is a
   PM2 fork that restarts on every deploy, and a panel summing raw counter
   values would show the total falling to zero and call it a quiet period.

The two judgement calls, "no recent execution" and "potentially stuck", are
documented at the constants that define them.
"""
import json

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}

# Every Prometheus query is scoped by these so the dashboard variables actually
# bind. A panel that silently ignores a variable is worse than one that has none.
SEL = 'environment=~"$environment", job_name=~"$job"'
SEL_STATUS = SEL + ', status=~"$status"'
SEL_ERR = SEL + ', error_type=~"$errortype"'
# See note 1 in the module docstring. instance_id is the application instance;
# `instance` on these series is the scrape target and means something else.
SEL_INST = SEL + ', instance_id=~"$instance"'

# Loki panels pin job="cron" (note 3) and read the pino fields, not labels.
# `| json | __error__=""` drops anything on the stream that is not our
# structured output; without it a stray unstructured line is counted as an
# event named "".
LOKI_BASE = '{job="cron"} | json | __error__=""'
LOKI_VARS = (' | jobName =~ `$job` | environment =~ `$environment`'
             ' | instanceId =~ `$instance` | trigger =~ `$trigger`')
# The four lifecycle events from instrumentCronJob: started, completed, failed,
# skipped. Backticks are a raw string in LogQL, so the dot needs no escaping
# gymnastics.
LOKI_EVENTS = ' | event =~ `cronjob\\..*`'

# How far back the self-calibrating thresholds look when they measure a job's
# own behaviour. Long enough that a weekly job has been seen at least once,
# short enough that a schedule change from a month ago is not still in effect.
HISTORY = "7d"
HISTORY_SECONDS = 7 * 24 * 3600

# ── "No recent execution" ────────────────────────────────────────
#
# The naive version is `time() - cronjob_last_run_timestamp_seconds > 3600`, and
# it is wrong in both directions at once: a job scheduled every minute has been
# broken for an hour before it trips, and a weekly job trips every single week
# while working perfectly.
#
# The obvious fix is to read the cron expression out of cronjob_info. It cannot
# be done: PromQL has no function that turns a label's string value into a
# number, so `schedule="*/5 * * * *"` is not something an expression can do
# arithmetic with. label_replace can rewrite the string but the result is still
# a label.
#
# So the expected interval is measured instead of parsed. Over HISTORY the job
# ran N times, so its mean gap is HISTORY/N, and the ratio below is "how many of
# its own normal gaps have passed since it last ran". One means it is due now,
# two means it has missed a turn. That self-calibrates: a minutely job and a
# weekly job are both flagged at the same ratio.
#
# What it does not catch, stated here and in the panel description:
#   - a job that has never run at all. It has no last_run timestamp and no runs
#     to count, so it is absent from this panel entirely. The Overview table is
#     where that shows up, as a row with a schedule and an empty last run.
#   - a job whose schedule was recently made more frequent. The mean gap still
#     reflects the old schedule until HISTORY rolls over.
#   - a job that runs irregularly by design. A mean gap is a poor summary of a
#     bursty schedule and this will read high between bursts.
# clamp_min keeps a job with zero runs in the window from dividing by zero.
OVERDUE = (
    f'(time() - cronjob_last_run_timestamp_seconds{{{SEL}}})'
    f' / on (job_name, environment) group_left() '
    f'({HISTORY_SECONDS} / clamp_min('
    f'sum by (job_name, environment) (increase(cronjob_runs_total{{{SEL}}}[{HISTORY}])), 1))'
)
OVERDUE_DESC = (
    "Ratio, not a duration: how many of this job's own typical gaps between runs have "
    "passed since it last started. 1 means it is due about now, 2 means it has missed a "
    "turn. The typical gap is measured, not read from the schedule, because PromQL "
    f"cannot turn the schedule label into a number; it is {HISTORY} divided by the number of "
    f"runs in the last {HISTORY}. Deliberate limits: a job that has never run does not appear "
    "here at all (look for it in the Overview table, with a schedule and no last run), a "
    f"job whose schedule got more frequent reads high until {HISTORY} of history rolls over, "
    "and a job that runs in bursts reads high between bursts. The schedule column is "
    "there so the ratio can be sanity checked against what was configured."
)

# ── "Potentially stuck" ──────────────────────────────────────────
#
# Same shape of problem, same refusal to pick one number. A run is compared
# against its own job's p95 duration rather than a fixed ceiling, so a job that
# normally takes 200ms and one that normally takes 20 minutes are held to their
# own standards.
#
# cronjob_last_run_timestamp_seconds is set when a run starts, so for a job with
# something in flight, time() minus it is roughly the current run's elapsed time.
# The `and on (...)` gate is what makes that true: without it the expression is
# just the age of the last run and every idle job looks infinitely stuck.
#
# The honest caveat, repeated in the panel description: the timestamp is the
# most recent start. When a job overlaps itself, an older run that really is
# hung is hidden behind the newer one's start time, so this under-reports
# exactly the jobs whose concurrency is above 1. Watch the concurrency panel
# next to it.
#
# clamp_min at 1 second stops a sub-second job from producing a ratio in the
# thousands the moment a run takes a normal-but-slower 3 seconds.
STUCK = (
    f'((time() - cronjob_last_run_timestamp_seconds{{{SEL}}})'
    f' / on (job_name, environment) group_left() '
    f'clamp_min(histogram_quantile(0.95, sum by (le, job_name, environment) '
    f'(rate(cronjob_duration_seconds_bucket{{{SEL}}}[{HISTORY}]))), 1))'
    f' and on (job_name, environment) '
    f'(max by (job_name, environment) (cronjob_concurrent_runs{{{SEL}}}) > 0)'
)
STUCK_DESC = (
    "Only jobs with a run in flight right now. The value is how many times its own p95 "
    f"duration (over {HISTORY}) the current run has already taken, so no single timeout is "
    "imposed on every job. Above 1 the run is slower than almost all of its history; "
    "above 3 it is worth looking at. Two caveats: the elapsed time comes from "
    "cronjob_last_run_timestamp_seconds, which is the most recent start, so a job "
    "overlapping itself hides an older hung run behind a newer start and this "
    "under-reports it, and the p95 floor is clamped to 1 second so sub-second jobs do "
    "not produce enormous ratios from a normal slow run."
)

# process.memoryUsage().rss is the whole Node process. Every memory panel says so.
MEM_DISCLAIMER = (
    "This is process.memoryUsage().rss for the entire cron process, not this job's own "
    "allocation. The cron process runs every job in one Node process and several of them "
    "overlap, so a figure attributed to one job includes whatever else was resident at the "
    "same moment. Read it as a correlation, never as an attribution: a job whose numbers "
    "climb while the others hold steady is a real lead, a single high reading is not."
)

panels = []
pid = 0


def nid():
    global pid
    pid += 1
    return pid


def prom(*exprs, legend=None, instant=False, fmt=None):
    out = []
    for i, e in enumerate(exprs):
        t = {"datasource": PROM, "expr": e, "refId": chr(65 + i), "editorMode": "code",
             "range": not instant, "instant": instant}
        if legend:
            t["legendFormat"] = legend[i] if isinstance(legend, list) else legend
        if fmt:
            t["format"] = fmt
        out.append(t)
    return out


def table_targets(*exprs):
    """Instant queries in table format, one refId each, for the joined tables."""
    return [{"datasource": PROM, "refId": chr(65 + i), "editorMode": "code",
             "instant": True, "range": False, "format": "table", "expr": e}
            for i, e in enumerate(exprs)]


def row(title, y):
    return {"type": "row", "title": title, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "id": nid(), "collapsed": False, "panels": []}


def stat(title, expr, x, y, w=4, h=4, unit="short", desc="", steps=None, legend="",
         dec=None, mappings=None):
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


def ts(title, exprs, legend, x, y, w=12, h=8, unit="short", desc="", stack=False,
       dec=None, fill=20, style="line"):
    return {
        "type": "timeseries", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": PROM,
        "targets": prom(*exprs, legend=legend),
        "options": {"legend": {"displayMode": "table", "placement": "bottom",
                               "showLegend": True, "calcs": ["mean", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "fieldConfig": {"defaults": {
            "unit": unit, "min": 0, "decimals": dec,
            "custom": {"drawStyle": style, "lineWidth": 1, "fillOpacity": fill,
                       "showPoints": "never", "spanNulls": True,
                       "stacking": {"mode": "normal" if stack else "none"}},
            "color": {"mode": "palette-classic"},
        }, "overrides": []},
    }


def ranked(title, expr, x, y, w=12, h=8, unit="short", desc=""):
    """Top-N bar gauge.

    Prometheus answers with one frame per series and calls the value field
    "Value" in every one of them, so the panel collapses them into a single
    unlabelled bar. labelsToFields promotes the grouping labels to real fields,
    merge folds the frames into one table, and values:true then draws one bar
    per row. Instant, not range: a range query re-evaluates the topk at every
    step and the panel receives ten series times every step instead of ten bars.
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


def logs(title, expr, x, y, w=24, h=12, desc=""):
    return {
        "type": "logs", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": LOKI,
        "targets": [{"datasource": LOKI, "expr": expr, "refId": "A",
                     "queryType": "range", "editorMode": "code"}],
        "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                    "enableLogDetails": True, "dedupStrategy": "none",
                    "prettifyLogMessage": False},
    }


# A join leaves one Time column per query, deduplicated as "Time", "Time 1" and
# so on, and none of them belong in the rendered table. Excluding a field that
# does not exist is a no-op, so the list is deliberately generous.
TIME_COLS = {"Time": True, **{f"Time {i}": True for i in range(1, 14)}}

STATUS_MAP = [{"type": "value", "options": {
    "success": {"text": "success", "color": "green", "index": 0},
    "partial": {"text": "partial", "color": "orange", "index": 1},
    "error": {"text": "error", "color": "red", "index": 2},
    "skipped": {"text": "skipped", "color": "blue", "index": 3},
    "cancelled": {"text": "cancelled", "color": "purple", "index": 4},
}}]

# Clicking a job name reloads the dashboard scoped to it, carrying the current
# time range. A relative URL keeps this working behind whatever domain Caddy
# serves Grafana on.
JOB_LINK = [{"title": "Filter this dashboard to ${__value.raw}",
             "url": "/d/pt-cronjobs/cronjobs-monitoring?var-job=${__value.raw}"
                    "&${__url_time_range}", "targetBlank": False}]


def override(name, props):
    return {"matcher": {"id": "byName", "options": name}, "properties": props}


# ── Overview ─────────────────────────────────────────────────────
y = 0
panels.append(row("Overview", y))
y += 1
panels.append(stat(
    "Jobs registered", f'count(count by (job_name) (cronjob_info{{{SEL}}}))', 0, y,
    desc="Distinct jobs that have registered with instrumentCronJob since the cron process "
         "last started. cronjob_info is set on the first run of each job, so a job that has "
         "not run since the last deploy is not counted here yet.",
    legend="jobs"))
panels.append(stat(
    "Runs in range",
    f'sum(increase(cronjob_runs_total{{{SEL_STATUS}}}[$__range])) or vector(0)', 4, y,
    desc="increase(), not the raw counter. The cron process is a PM2 fork that restarts on "
         "every deploy and resets its counters; summing the raw values would draw the total "
         "collapsing to zero and read as a quiet period.",
    legend="runs", dec=0))
panels.append(stat(
    "Success rate (range)",
    # Skipped and cancelled runs are excluded from the denominator on purpose: a
    # job that declined to run because it was already running has not failed,
    # and counting it as a failure makes a healthy lock look like an incident.
    f'sum(increase(cronjob_runs_total{{{SEL}, status="success"}}[$__range]))'
    f' / clamp_min(sum(increase(cronjob_runs_total{{{SEL}, '
    f'status=~"success|error|partial"}}[$__range])), 1)',
    8, y, unit="percentunit",
    desc="Successful runs over runs that actually attempted work. skipped and cancelled are "
         "left out of the denominator, because declining to run is not a failure. partial "
         "counts against the rate: a run that processed some items and failed others is not "
         "a success. The $status variable deliberately does not apply here, since filtering "
         "the numerator and denominator by status would make the ratio meaningless.",
    steps=[{"color": "red", "value": None}, {"color": "orange", "value": 0.9},
           {"color": "green", "value": 0.99}],
    legend="success rate"))
panels.append(stat(
    "Failed runs in range",
    f'sum(increase(cronjob_runs_total{{{SEL}, status=~"error|partial"}}[$__range])) '
    f'or vector(0)', 12, y,
    desc="Runs that ended error or partial. `or vector(0)` matters: with no failures the "
         "selector matches no series and the panel would paint \"No data\", which looks "
         "identical to a broken scrape in exactly the healthy case.",
    steps=[{"color": "green", "value": None}, {"color": "orange", "value": 1}],
    legend="failed", dec=0))
panels.append(stat(
    "Running now", f'sum(cronjob_concurrent_runs{{{SEL}}}) or vector(0)', 16, y,
    desc="A gauge, so it is read raw. Incremented when a run starts and decremented in a "
         "finally block, so a throw cannot leave it stuck above zero. A number that never "
         "returns to zero on an idle schedule means the process died mid-run.",
    legend="in flight", dec=0))
panels.append(stat(
    "Jobs overdue", f'count({OVERDUE} > 2) or vector(0)', 20, y,
    desc="Jobs that have gone more than twice their own typical gap without running. " +
         OVERDUE_DESC,
    steps=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
    legend="overdue", dec=0))

y += 4
panels.append({
    "type": "table", "title": "Jobs", "id": nid(),
    "description":
        "One row per job, built by joining eleven instant queries on job_name. The "
        "schedule column comes from cronjob_info, which is the only place the configured "
        "cron expression exists; it is joined rather than assumed, so a job that is "
        "registered but has never completed still gets a row with a schedule and empty "
        "timestamps. There is deliberately no \"last duration\" column: "
        "cronjob_duration_seconds is a histogram and carries no per-run value, so anything "
        "in that column would be an average over some window wearing the label of a single "
        "run, and it would mean different things on different rows depending on how often "
        "the job runs. Mean and p95 over the dashboard's time range are shown instead, and "
        "the true duration of one specific run is on its cronjob.completed line in the Logs "
        "section, as durationMs. Success rate excludes skipped and cancelled from its "
        "denominator and ignores $status, for the reason given on the Success rate stat. "
        "Click a job name to scope the whole dashboard to it.",
    "gridPos": {"h": 12, "w": 24, "x": 0, "y": y}, "datasource": PROM,
    "targets": table_targets(
        # A: the schedule. max by (..., schedule) keeps the label as a column.
        f'max by (job_name, schedule) (cronjob_info{{{SEL}}})',
        # B: `== 1` selects the single status series that is set, so the status
        # arrives as a label to colour on rather than a numeric code the
        # dashboard would have to keep a mapping for.
        f'max by (job_name, status) (cronjob_last_run_status{{{SEL}}} == 1)',
        # C, D, E: epoch seconds times 1000, because Grafana's dateTimeAsIso
        # unit reads milliseconds.
        f'max by (job_name) (cronjob_last_run_timestamp_seconds{{{SEL}}}) * 1000',
        f'max by (job_name) (cronjob_last_success_timestamp_seconds{{{SEL}}}) * 1000',
        f'max by (job_name) (cronjob_last_error_timestamp_seconds{{{SEL}}}) * 1000',
        f'time() - max by (job_name) (cronjob_last_run_timestamp_seconds{{{SEL}}})',
        # G, H: mean and p95 over the dashboard range. Both are averages of many
        # runs and are named as such; see the note above on why no column here
        # claims to be a single run's duration.
        f'sum by (job_name) (increase(cronjob_duration_seconds_sum{{{SEL_STATUS}}}[$__range]))'
        f' / sum by (job_name) '
        f'(increase(cronjob_duration_seconds_count{{{SEL_STATUS}}}[$__range]))',
        f'histogram_quantile(0.95, sum by (le, job_name) '
        f'(rate(cronjob_duration_seconds_bucket{{{SEL_STATUS}}}[$__range])))',
        f'sum by (job_name) (increase(cronjob_runs_total{{{SEL}, status="success"}}[$__range]))'
        f' / clamp_min(sum by (job_name) (increase(cronjob_runs_total{{{SEL}, '
        f'status=~"success|error|partial"}}[$__range])), 1)',
        f'sum by (job_name) (increase(cronjob_errors_total{{{SEL_ERR}}}[$__range]))',
        f'sum by (job_name) (cronjob_concurrent_runs{{{SEL}}})',
    ),
    # outer, not inner: a job registered but never run has no timestamp series,
    # and an inner join would drop exactly the row worth seeing.
    "transformations": [
        {"id": "joinByField", "options": {"byField": "job_name", "mode": "outer"}},
        {"id": "organize", "options": {
            "excludeByName": {**TIME_COLS, "Value #A": True, "Value #B": True},
            "renameByName": {
                "job_name": "Job", "schedule": "Schedule", "status": "Last status",
                "Value #C": "Last run", "Value #D": "Last success", "Value #E": "Last error",
                "Value #F": "Since last run", "Value #G": "Mean duration",
                "Value #H": "p95 duration", "Value #I": "Success rate",
                "Value #J": "Errors", "Value #K": "Running",
            },
            "indexByName": {
                "job_name": 0, "schedule": 1, "status": 2, "Value #C": 3, "Value #D": 4,
                "Value #E": 5, "Value #F": 6, "Value #G": 7, "Value #H": 8, "Value #I": 9,
                "Value #J": 10, "Value #K": 11,
            },
        }},
    ],
    "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False},
                "sortBy": [{"displayName": "Since last run", "desc": True}]},
    "fieldConfig": {
        "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"},
                                "filterable": True}},
        # Overrides match the post-transformation display names, which is why
        # these use the renamed headings rather than the Value #x refIds.
        "overrides": [
            override("Job", [{"id": "links", "value": JOB_LINK},
                             {"id": "custom.width", "value": 200}]),
            override("Schedule", [{"id": "custom.width", "value": 120}]),
            override("Last status", [
                {"id": "mappings", "value": STATUS_MAP},
                {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                {"id": "custom.width", "value": 110}]),
            override("Last run", [{"id": "unit", "value": "dateTimeAsIso"}]),
            override("Last success", [{"id": "unit", "value": "dateTimeAsIso"}]),
            override("Last error", [{"id": "unit", "value": "dateTimeAsIso"}]),
            override("Since last run", [{"id": "unit", "value": "s"},
                                        {"id": "decimals", "value": 0}]),
            override("Mean duration", [{"id": "unit", "value": "s"},
                                       {"id": "decimals", "value": 2}]),
            override("p95 duration", [{"id": "unit", "value": "s"},
                                      {"id": "decimals", "value": 2}]),
            override("Success rate", [
                {"id": "unit", "value": "percentunit"},
                {"id": "decimals", "value": 3},
                {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                    {"color": "red", "value": None}, {"color": "orange", "value": 0.9},
                    {"color": "green", "value": 0.99}]}}]),
            override("Errors", [
                {"id": "decimals", "value": 0},
                {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                    {"color": "green", "value": None}, {"color": "orange", "value": 1}]}}]),
            override("Running", [{"id": "decimals", "value": 0},
                                 {"id": "custom.width", "value": 90}]),
        ],
    },
})

# ── Executions ───────────────────────────────────────────────────
y += 12
panels.append(row("Executions", y))
y += 1
panels.append(ts(
    "Runs per hour by status",
    [f'sum by (status) (rate(cronjob_runs_total{{{SEL_STATUS}}}[$__rate_interval])) * 3600'],
    "{{status}}", 0, y, stack=True,
    desc="rate() scaled to an hour, because a per-second run rate for a job that fires every "
         "five minutes is a number with four leading zeros. $__rate_interval rather than a "
         "fixed window, so the rate stays defined when the dashboard is zoomed out and the "
         "step grows past it. Stacked, so the total height is the overall run rate and the "
         "red band is the part of it that failed."))
panels.append(ts(
    "Runs per hour by job",
    [f'sum by (job_name) (rate(cronjob_runs_total{{{SEL_STATUS}}}[$__rate_interval])) * 3600'],
    "{{job_name}}", 12, y,
    desc="Observed frequency per job. Worth comparing against the Schedule column in the "
         "Overview table: a job running at half its configured frequency is either "
         "overlapping and being skipped, or not being triggered at all."))
y += 8
panels.append(ranked(
    "Executions in range by job",
    f'topk(10, sum by (job_name) (increase(cronjob_runs_total{{{SEL_STATUS}}}[$__range])))',
    0, y, desc="Total runs over the dashboard's time range, top ten jobs. Instant evaluation "
               "of the topk, so this is ten bars rather than a ranking recomputed at every "
               "step."))
panels.append(ts(
    "Concurrent runs",
    [f'sum by (job_name) (cronjob_concurrent_runs{{{SEL}}})',
     f'sum by (job_name, instance_id) (cronjob_running{{{SEL_INST}}})'],
    ["{{job_name}} (process)", "{{job_name}} on instance {{instance_id}}"],
    12, y, style="stepAfter", fill=10,
    desc="Two gauges for what looks like one number. cronjob_concurrent_runs is the "
         "per-process total; cronjob_running carries the application instance, so a job "
         "running on two instances at once is visible instead of being averaged away. That "
         "label is instance_id, not instance: `instance` on these series is the scrape "
         "target Prometheus attached, which is a different thing and is the same for every "
         "job. Drawn as steps because a gauge holds its value between scrapes rather than "
         "sloping between them."))
y += 8
panels.append(ts(
    "Skipped runs per hour by reason",
    [f'sum by (reason) (rate(cronjob_skipped_runs_total{{{SEL}}}[$__rate_interval])) * 3600'],
    "{{reason}}", 0, y, stack=True,
    desc="A skip is not a failure. already_running and lock_unavailable are the guard working "
         "as designed, and their rate rising means runs are starting to overlap, which is the "
         "early warning that a job is outgrowing its schedule. disabled and "
         "dependency_unavailable are decisions taken elsewhere. The reason set is closed in "
         "instrumentCronJob, so anything unrecognised is recorded as manual_skip."))
panels.append(ranked(
    "Skipped runs in range by job",
    f'topk(10, sum by (job_name, reason) '
    f'(increase(cronjob_skipped_runs_total{{{SEL}}}[$__range])))',
    12, y, desc="Which jobs are declining to run, and why. A job dominated by already_running "
                "is being scheduled faster than it completes."))

# ── Status and reliability ───────────────────────────────────────
y += 8
panels.append(row("Status and reliability", y))
y += 1
panels.append({
    "type": "table", "title": "No recent execution", "id": nid(),
    "description": OVERDUE_DESC,
    "gridPos": {"h": 8, "w": 12, "x": 0, "y": y}, "datasource": PROM,
    "targets": table_targets(
        OVERDUE,
        f'time() - max by (job_name) (cronjob_last_run_timestamp_seconds{{{SEL}}})',
        f'max by (job_name, schedule) (cronjob_info{{{SEL}}})',
    ),
    "transformations": [
        {"id": "joinByField", "options": {"byField": "job_name", "mode": "outer"}},
        {"id": "organize", "options": {
            # OVERDUE keeps the left side's labels, so environment, instance and
            # job ride along as columns and are dropped here.
            "excludeByName": {**TIME_COLS, "Value #C": True, "environment": True,
                              "instance": True, "job": True},
            "renameByName": {"job_name": "Job", "schedule": "Schedule",
                             "Value #A": "Gaps missed", "Value #B": "Since last run"},
            "indexByName": {"job_name": 0, "schedule": 1, "Value #B": 2, "Value #A": 3},
        }},
        {"id": "sortBy", "options": {"sort": [{"field": "Gaps missed", "desc": True}]}},
    ],
    "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
    "fieldConfig": {
        "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
        "overrides": [
            override("Job", [{"id": "links", "value": JOB_LINK}]),
            override("Since last run", [{"id": "unit", "value": "s"},
                                        {"id": "decimals", "value": 0}]),
            override("Gaps missed", [
                {"id": "decimals", "value": 2},
                {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                    {"color": "green", "value": None}, {"color": "orange", "value": 2},
                    {"color": "red", "value": 4}]}}]),
        ],
    },
})
panels.append({
    "type": "table", "title": "Potentially stuck", "id": nid(),
    "description": STUCK_DESC,
    "gridPos": {"h": 8, "w": 12, "x": 12, "y": y}, "datasource": PROM,
    "targets": table_targets(
        STUCK,
        f'histogram_quantile(0.95, sum by (le, job_name) '
        f'(rate(cronjob_duration_seconds_bucket{{{SEL}}}[{HISTORY}])))',
        f'sum by (job_name) (cronjob_concurrent_runs{{{SEL}}})',
    ),
    "transformations": [
        {"id": "joinByField", "options": {"byField": "job_name", "mode": "outer"}},
        {"id": "organize", "options": {
            "excludeByName": {**TIME_COLS, "environment": True, "instance": True,
                              "job": True},
            "renameByName": {"job_name": "Job", "Value #A": "Over own p95",
                             "Value #B": "p95 duration", "Value #C": "In flight"},
            "indexByName": {"job_name": 0, "Value #C": 1, "Value #B": 2, "Value #A": 3},
        }},
        {"id": "sortBy", "options": {"sort": [{"field": "Over own p95", "desc": True}]}},
    ],
    "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
    "fieldConfig": {
        "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
        "overrides": [
            override("Job", [{"id": "links", "value": JOB_LINK}]),
            override("p95 duration", [{"id": "unit", "value": "s"},
                                      {"id": "decimals", "value": 2}]),
            override("In flight", [{"id": "decimals", "value": 0}]),
            override("Over own p95", [
                {"id": "decimals", "value": 2},
                {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                {"id": "thresholds", "value": {"mode": "absolute", "steps": [
                    {"color": "green", "value": None}, {"color": "orange", "value": 1},
                    {"color": "red", "value": 3}]}}]),
        ],
    },
})
y += 8
panels.append(ts(
    "Success rate by job",
    [f'sum by (job_name) (rate(cronjob_runs_total{{{SEL}, status="success"}}[$__rate_interval]))'
     f' / sum by (job_name) (rate(cronjob_runs_total{{{SEL}, '
     f'status=~"success|error|partial"}}[$__rate_interval]))'],
    "{{job_name}}", 0, y, unit="percentunit", dec=3,
    desc="Same definition as the Overview stat: skipped and cancelled are out of the "
         "denominator, partial counts as a failure. A job with no attempted runs in a step "
         "divides zero by zero and leaves a gap, which is correct: there was nothing to have "
         "a success rate about."))
panels.append(ts(
    "Time since last success",
    [f'time() - cronjob_last_success_timestamp_seconds{{{SEL}}}'],
    "{{job_name}}", 12, y, unit="s",
    desc="Rises on a straight line while a job is failing and drops to zero the moment it "
         "succeeds, so the sawtooth is normal and a line that only climbs is not. This is "
         "the panel that separates \"failing loudly\" from \"failing and nobody noticed\": a "
         "job erroring every five minutes and a job that stopped being triggered look "
         "identical on an error rate chart and completely different here."))
y += 8
panels.append({
    "type": "table", "title": "Last run status", "id": nid(),
    "description":
        "One series per status is exported, with only the current one set to 1, so `== 1` "
        "returns the status as a label rather than a numeric code the dashboard would have "
        "to keep a value mapping for. A job missing from this table has registered but has "
        "not completed a run since the process started.",
    "gridPos": {"h": 6, "w": 24, "x": 0, "y": y}, "datasource": PROM,
    "targets": table_targets(
        f'max by (job_name, status, environment) '
        f'(cronjob_last_run_status{{{SEL_STATUS}}} == 1)',
    ),
    "transformations": [
        {"id": "organize", "options": {
            "excludeByName": {**TIME_COLS, "Value #A": True, "Value": True},
            "renameByName": {"job_name": "Job", "status": "Last status",
                             "environment": "Environment"},
            "indexByName": {"job_name": 0, "status": 1, "environment": 2},
        }},
    ],
    "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
    "fieldConfig": {
        "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"},
                                "filterable": True}},
        "overrides": [
            override("Job", [{"id": "links", "value": JOB_LINK}]),
            override("Last status", [
                {"id": "mappings", "value": STATUS_MAP},
                {"id": "custom.cellOptions", "value": {"type": "color-background"}}]),
        ],
    },
})

# ── Performance ──────────────────────────────────────────────────
y += 6
panels.append(row("Performance", y))
y += 1
panels.append(ts(
    "Duration p50 / p95 / p99",
    [f'histogram_quantile(0.50, sum by (le) '
     f'(rate(cronjob_duration_seconds_bucket{{{SEL_STATUS}}}[$__rate_interval])))',
     f'histogram_quantile(0.95, sum by (le) '
     f'(rate(cronjob_duration_seconds_bucket{{{SEL_STATUS}}}[$__rate_interval])))',
     f'histogram_quantile(0.99, sum by (le) '
     f'(rate(cronjob_duration_seconds_bucket{{{SEL_STATUS}}}[$__rate_interval])))'],
    ["p50", "p95", "p99"], 0, y, unit="s",
    desc="All jobs combined, which is only meaningful with the job variable narrowed: the "
         "buckets stretch to 30 minutes to cover the LearnUpon reconciliation, so a "
         "combined p99 is dominated by whichever job is slowest rather than by a change in "
         "any of them. Use it to spot a shift, then use the by-job panel to find which one."))
panels.append(ts(
    "p95 duration by job",
    [f'histogram_quantile(0.95, sum by (le, job_name) '
     f'(rate(cronjob_duration_seconds_bucket{{{SEL_STATUS}}}[$__rate_interval])))'],
    "{{job_name}}", 12, y, unit="s",
    desc="The panel the stuck-job threshold is derived from. Quantiles from a histogram are "
         "interpolated inside a bucket, so a p95 sitting exactly on a bucket edge is the "
         "bucket boundary rather than a measurement; the edges here are 0.05, 0.1, 0.5, 1, "
         "2.5, 5, 10, 30, 60, 120, 300, 600 and 1800 seconds."))
y += 8
panels.append(ts(
    "Mean duration by job",
    [f'sum by (job_name) (rate(cronjob_duration_seconds_sum{{{SEL_STATUS}}}[$__rate_interval]))'
     f' / sum by (job_name) '
     f'(rate(cronjob_duration_seconds_count{{{SEL_STATUS}}}[$__rate_interval]))'],
    "{{job_name}}", 0, y, unit="s",
    desc="Sum over count, both as rates, which is the only division that survives a counter "
         "reset: the raw _sum and _count both drop to zero on a restart, and their ratio "
         "would be undefined for the step that spans it. Worth reading next to the p95: a "
         "mean that tracks the p95 means uniform runs, a mean far below it means a tail."))
panels.append(ts(
    "Time spent running, per job",
    [f'sum by (job_name) '
     f'(rate(cronjob_duration_seconds_sum{{{SEL_STATUS}}}[$__rate_interval]))'],
    "{{job_name}}", 12, y, unit="percentunit", stack=True,
    desc="Seconds of execution per second of wall clock, which is the fraction of the time "
         "this job is running at all. Stacked, so the total is how busy the cron process is: "
         "a total approaching 1 means it is essentially never idle, and above 1 means jobs "
         "are overlapping. This is the number that says a schedule is too tight, and it "
         "needs no threshold to interpret."))
y += 8
panels.append({
    "type": "heatmap", "title": "Duration distribution", "id": nid(),
    "description":
        "The histogram buckets themselves rather than a quantile of them, so a bimodal job "
        "is visible as two bands instead of being averaged into a single misleading middle. "
        "Read with the bucket edges in mind: the rows are the declared boundaries, not a "
        "linear scale, and the widest row spans 600 to 1800 seconds.",
    "gridPos": {"h": 9, "w": 24, "x": 0, "y": y}, "datasource": PROM,
    "targets": [{"datasource": PROM, "refId": "A", "editorMode": "code", "range": True,
                 "format": "heatmap", "legendFormat": "{{le}}",
                 "expr": f'sum by (le) '
                         f'(increase(cronjob_duration_seconds_bucket{{{SEL_STATUS}}}'
                         f'[$__interval]))'}],
    "options": {
        "calculate": False,
        "cellGap": 1,
        "color": {"mode": "scheme", "scheme": "Oranges", "steps": 64, "reverse": False,
                  "exponent": 0.5, "fill": "dark-orange"},
        "yAxis": {"unit": "s", "axisPlacement": "left"},
        "legend": {"show": True},
        "tooltip": {"mode": "single", "yHistogram": True},
        "exemplars": {"color": "rgba(255,0,255,0.7)"},
        "filterValues": {"le": 1e-09},
    },
    "fieldConfig": {"defaults": {"custom": {"hideFrom": {"tooltip": False, "viz": False,
                                                         "legend": False}}},
                    "overrides": []},
})

# ── Memory and resources ─────────────────────────────────────────
y += 9
panels.append(row("Memory and resources", y))
y += 1
panels.append(ts(
    "p95 RSS after a run, by job",
    [f'histogram_quantile(0.95, sum by (le, job_name) '
     f'(rate(cronjob_memory_usage_bytes_bucket{{{SEL}, measurement="after"}}'
     f'[$__rate_interval])))'],
    "{{job_name}}", 0, y, w=8, unit="bytes",
    desc="Resident memory sampled when a run finished. " + MEM_DISCLAIMER))
panels.append(ts(
    "p95 peak RSS during a run, by job",
    [f'histogram_quantile(0.95, sum by (le, job_name) '
     f'(rate(cronjob_memory_usage_bytes_bucket{{{SEL}, measurement="peak"}}'
     f'[$__rate_interval])))'],
    "{{job_name}}", 8, y, w=8, unit="bytes",
    desc="The highest reading seen during a run, which only moves if the job calls "
         "ctx.sampleMemory(); a job that never samples reports its start and end readings "
         "as the peak and will look flatter than it is. " + MEM_DISCLAIMER))
panels.append(ts(
    "p95 RSS growth across a run, by job",
    [f'histogram_quantile(0.95, sum by (le, job_name) '
     f'(rate(cronjob_memory_usage_bytes_bucket{{{SEL}, measurement="delta"}}'
     f'[$__rate_interval])))'],
    "{{job_name}}", 16, y, w=8, unit="bytes",
    desc="After minus before, and only when it grew: a histogram cannot hold a negative "
         "sample, so runs that released memory are not recorded here at all and this is a "
         "one-sided view by construction. Growth is also not a leak, since V8 may simply not "
         "have collected yet. " + MEM_DISCLAIMER))
y += 8
panels.append(ranked(
    "Mean RSS growth per run, by job",
    # sum/count rather than a quantile: the question here is the typical run,
    # and averaging the histogram's sum over its count is exactly that.
    f'topk(10, sum by (job_name) '
    f'(increase(cronjob_memory_usage_bytes_sum{{{SEL}, measurement="delta"}}[$__range]))'
    f' / sum by (job_name) '
    f'(increase(cronjob_memory_usage_bytes_count{{{SEL}, measurement="delta"}}[$__range])))',
    0, y, w=24, unit="bytes",
    desc="Mean growth over the time range, top ten jobs. A ranking of which jobs are around "
         "when the process grows, which is not the same as which jobs caused it. " +
         MEM_DISCLAIMER))

# ── Processing ───────────────────────────────────────────────────
y += 8
panels.append(row("Processing", y))
y += 1
panels.append(ts(
    "Items processed per second, by result",
    [f'sum by (result) (rate(cronjob_items_processed_total{{{SEL}}}[$__rate_interval]))'],
    "{{result}}", 0, y, stack=True,
    desc="Per-item outcomes, which are a different thing from run outcomes: a run that ends "
         "partial has both successes and errors here, and a run that ends error may have "
         "processed nothing at all. Only jobs that call ctx.item() or return totals appear."))
panels.append(ts(
    "Items processed per second, by job",
    [f'sum by (job_name) (rate(cronjob_items_processed_total{{{SEL}}}[$__rate_interval]))'],
    "{{job_name}}", 12, y,
    desc="Throughput per job. A job whose run rate is unchanged while this falls is still "
         "being triggered but finding less to do, which is usually upstream."))
y += 8
panels.append(ts(
    "Items per run, by job",
    [f'sum by (job_name) (rate(cronjob_items_processed_total{{{SEL}}}[$__rate_interval]))'
     f' / sum by (job_name) (rate(cronjob_runs_total{{{SEL}}}[$__rate_interval]))'],
    "{{job_name}}", 0, y,
    desc="Batch size, derived from two rates rather than two raw counters so a process "
         "restart cannot make it jump. Where a job had no runs in a step this divides zero "
         "by zero and leaves a gap rather than drawing a spike."))
panels.append(ts(
    "Item failure ratio, by job",
    [f'sum by (job_name) '
     f'(rate(cronjob_items_processed_total{{{SEL}, result="error"}}[$__rate_interval]))'
     f' / sum by (job_name) (rate(cronjob_items_processed_total{{{SEL}}}[$__rate_interval]))'],
    "{{job_name}}", 12, y, unit="percentunit", dec=3,
    desc="The share of individual records that failed. This is the panel that catches the "
         "slow poisoning case: a job that keeps ending success while a growing fraction of "
         "its records fail, which no run-level metric will show."))

# ── Errors ───────────────────────────────────────────────────────
y += 8
panels.append(row("Errors", y))
y += 1
panels.append(ts(
    "Errors per hour by type",
    [f'sum by (error_type) (rate(cronjob_errors_total{{{SEL_ERR}}}[$__rate_interval])) * 3600'],
    "{{error_type}}", 0, y, stack=True,
    desc="The type set is closed on purpose: an error message is unbounded and a stack trace "
         "worse, so classifyError() matches the shape of the error instead. The consequence "
         "is that anything unrecognised lands in `unexpected`, and a rising unexpected share "
         "is the signal to add a branch to classifyError, not to widen the label."))
panels.append(ranked(
    "Errors in range by job and type",
    f'topk(10, sum by (job_name, error_type) '
    f'(increase(cronjob_errors_total{{{SEL_ERR}}}[$__range])))',
    12, y,
    desc="Where the failures are concentrated. Counted per failed run, not per failed item: "
         "a run that failed five hundred records once is one error here."))
y += 8
panels.append(ts(
    "Errors per hour by job",
    [f'sum by (job_name) (rate(cronjob_errors_total{{{SEL_ERR}}}[$__rate_interval])) * 3600'],
    "{{job_name}}", 0, y,
    desc="cronjob_errors_total only increments for status=error. A run that ended partial "
         "raised no exception and is absent here, so read this next to the partial band on "
         "Runs per hour by status."))
panels.append({
    "type": "table", "title": "Last error per job", "id": nid(),
    "description":
        "When each job most recently failed, and how long ago. A job whose last error is old "
        "and whose last success is newer has recovered; a job whose last error is newer than "
        "its last success has not. Click the job name to scope the dashboard, then read the "
        "Logs section below for the failure lines.",
    "gridPos": {"h": 8, "w": 12, "x": 12, "y": y}, "datasource": PROM,
    "targets": table_targets(
        f'max by (job_name) (cronjob_last_error_timestamp_seconds{{{SEL}}}) * 1000',
        f'time() - max by (job_name) (cronjob_last_error_timestamp_seconds{{{SEL}}})',
        f'max by (job_name) (cronjob_last_success_timestamp_seconds{{{SEL}}}) * 1000',
        f'sum by (job_name) (increase(cronjob_errors_total{{{SEL_ERR}}}[$__range]))',
    ),
    "transformations": [
        {"id": "joinByField", "options": {"byField": "job_name", "mode": "outer"}},
        {"id": "organize", "options": {
            "excludeByName": {**TIME_COLS},
            "renameByName": {"job_name": "Job", "Value #A": "Last error",
                             "Value #B": "Since last error", "Value #C": "Last success",
                             "Value #D": "Errors in range"},
            "indexByName": {"job_name": 0, "Value #A": 1, "Value #B": 2, "Value #C": 3,
                            "Value #D": 4},
        }},
        {"id": "sortBy", "options": {"sort": [{"field": "Since last error", "desc": False}]}},
    ],
    "options": {"showHeader": True, "cellHeight": "sm", "footer": {"show": False}},
    "fieldConfig": {
        "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
        "overrides": [
            override("Job", [{"id": "links", "value": JOB_LINK}]),
            override("Last error", [{"id": "unit", "value": "dateTimeAsIso"}]),
            override("Last success", [{"id": "unit", "value": "dateTimeAsIso"}]),
            override("Since last error", [{"id": "unit", "value": "s"},
                                          {"id": "decimals", "value": 0}]),
            override("Errors in range", [{"id": "decimals", "value": 0}]),
        ],
    },
})

# ── Logs ─────────────────────────────────────────────────────────
y += 8
panels.append(row("Logs", y))
y += 1
panels.append({
    "type": "timeseries", "title": "Lifecycle events over time", "id": nid(),
    "description":
        "Counted from the logs rather than the metrics, which makes this the cross-check on "
        "everything above: if Prometheus says a job ran and no cronjob.started line exists "
        "for it, one of the two is wrong. cronjob.started fires at the top of every run, and "
        "exactly one of completed, failed or skipped fires at the end, so started running "
        "ahead of the other three means runs are being abandoned mid-flight.",
    "gridPos": {"h": 6, "w": 24, "x": 0, "y": y}, "datasource": LOKI,
    "targets": [{"datasource": LOKI, "refId": "A", "queryType": "range", "editorMode": "code",
                 "legendFormat": "{{event}}",
                 "expr": f'sum by (event) (count_over_time({LOKI_BASE}{LOKI_EVENTS}'
                         f'{LOKI_VARS} [$__auto]))'}],
    "options": {"legend": {"displayMode": "table", "placement": "bottom", "showLegend": True,
                           "calcs": ["sum"]},
                "tooltip": {"mode": "multi", "sort": "desc"}},
    "fieldConfig": {"defaults": {
        "unit": "short", "min": 0,
        "custom": {"drawStyle": "bars", "lineWidth": 1, "fillOpacity": 60,
                   "showPoints": "never", "stacking": {"mode": "normal"}},
        "color": {"mode": "palette-classic"},
    }, "overrides": []},
})
y += 6
panels.append(logs(
    "Failed and skipped runs",
    f'{LOKI_BASE} | event =~ `cronjob\\.(failed|skipped)`{LOKI_VARS}'
    f' | status =~ `$status` | errorType =~ `$errortype`',
    0, y, h=10,
    desc="The end-of-run line for every run that did not succeed. It carries the whole "
         "summary the wrapper assembled: durationMs, the processed/succeeded/failed/skipped "
         "counts, the memory readings, errorType and, on a failure, the serialised error "
         "with its stack. Expand a line and copy its jobRunId into the Run id box at the top "
         "of the dashboard to pull up every line that run produced, in the panel below."))
y += 10
panels.append(logs(
    "Run trace",
    # Deliberately no event filter. The lifecycle lines are only part of a run's
    # output: jobRunId reaches the job's own ctx.log calls through the childLogger
    # binding, and those are usually the lines that explain a failure. Filtering
    # on event here would hide exactly what the drill-down is for.
    #
    # `|= "$runid"` is a line filter, and an empty one matches everything, so with
    # the box blank this is simply every cron job line.
    f'{{job="cron"}} |= "$runid" | json | __error__="" | jobName =~ `$job`'
    f' | environment =~ `$environment` | instanceId =~ `$instance`',
    0, y, h=12,
    desc="Paste a jobRunId into the Run id box to see one execution end to end: the "
         "cronjob.started line, everything the job itself logged through ctx.log, and the "
         "terminal cronjob.completed / failed / skipped line. jobRunId is a field and never "
         "a label, because one series per execution would grow without bound, so this is a "
         "line filter on the raw text followed by the JSON parser rather than a stream "
         "selector. With the box empty it shows every cron job line matching the variables. "
         "The trigger variable is not applied here, so that a run's own log lines are never "
         "filtered out of their own trace."))

dashboard = {
    "uid": "pt-cronjobs",
    "title": "Cronjobs Monitoring",
    "description":
        "Execution, reliability, duration, memory, throughput and errors for every job "
        "wrapped in instrumentCronJob. Metrics come from backend/utils/cronMetrics.js and "
        "logs from backend/utils/instrumentCronJob.js in the performance-tracker repo. "
        "Only the cron process emits these, so everything here is scoped to it. Generated "
        "by grafana/dashboards/build-cronjobs.py: edit the generator, not this JSON.",
    "tags": ["performance-tracker", "observability", "cronjobs"],
    "timezone": "browser",
    "editable": True,
    "schemaVersion": 39,
    "version": 1,
    "refresh": "1m",
    "time": {"from": "now-24h", "to": "now"},
    "panels": panels,
    "templating": {"list": [
        {
            "name": "environment", "label": "Environment", "type": "query", "datasource": PROM,
            "query": {"query": "label_values(cronjob_info, environment)", "refId": "A"},
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]}, "refresh": 1,
        },
        {
            # job_name, not job. `job` in both Prometheus and Loki is the process
            # ("cron"), which is a different thing and is pinned rather than
            # varied here. The variable is still called $job because the cron job
            # is what anyone reading this dashboard means by the word.
            "name": "job", "label": "Cron job", "type": "query", "datasource": PROM,
            "query": {"query": "label_values(cronjob_info, job_name)", "refId": "A"},
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]}, "refresh": 1,
        },
        {
            "name": "status", "label": "Run status", "type": "custom",
            # Custom rather than a query, so all five appear even before one has
            # occurred. A status that has never happened has no series to
            # enumerate, and `cancelled` is in the metric's label set with
            # nothing producing it today.
            "query": "success,error,partial,skipped,cancelled",
            "options": [{"text": s, "value": s, "selected": False}
                        for s in ["success", "error", "partial", "skipped", "cancelled"]],
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]},
        },
        {
            # instance_id. See note 1 in the module docstring: enumerating
            # `instance` here would list the scrape target address, which is the
            # same for every job and is not what this filter is for. In Loki the
            # same value is the instanceId field, which is why the log panels
            # match on that.
            "name": "instance", "label": "Instance", "type": "query", "datasource": PROM,
            "query": {"query": "label_values(cronjob_running, instance_id)", "refId": "A"},
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]}, "refresh": 1,
        },
        {
            # Custom because trigger is not a metric label anywhere: it lives
            # only in the childLogger bindings, so it can only filter the Loki
            # panels. The four values are the closed set in instrumentCronJob.
            "name": "trigger", "label": "Trigger (logs only)", "type": "custom",
            "query": "scheduled,manual,retry,startup",
            "options": [{"text": t, "value": t, "selected": False}
                        for t in ["scheduled", "manual", "retry", "startup"]],
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]},
        },
        {
            # Custom for the same reason as status: the eight types are the
            # closed set classifyError can return, and listing only the ones
            # that have already happened hides which are possible.
            "name": "errortype", "label": "Error type", "type": "custom",
            "query": "timeout,database,connection,validation,rate_limit,authentication,"
                     "external_api,unexpected",
            "options": [{"text": e, "value": e, "selected": False} for e in [
                "timeout", "database", "connection", "validation", "rate_limit",
                "authentication", "external_api", "unexpected"]],
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]},
        },
        {
            # The drill-down. jobRunId cannot be a variable sourced from a label
            # because it is not one, so it is a free text box feeding a line
            # filter. Empty by default, and an empty LogQL line filter matches
            # everything, so a blank box is not an empty dashboard.
            "name": "runid", "label": "Run id", "type": "textbox",
            "query": "", "current": {"text": "", "value": ""},
        },
    ]},
    "annotations": {"list": []},
}

with open("grafana/dashboards/cronjobs.json", "w") as f:
    json.dump(dashboard, f, indent=2)
    f.write("\n")

print("wrote grafana/dashboards/cronjobs.json")
print("panels:", len([p for p in panels if p["type"] != "row"]),
      "+", len([p for p in panels if p["type"] == "row"]), "rows")
