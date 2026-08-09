"""Generate the CI and Deploys dashboard.

Run from the repo root:  python3 grafana/dashboards/build-ci-deploys.py

Two sources, and the split between them is the whole point of this dashboard.

`app_build_info` and `app_start_timestamp_seconds` come from the applications
themselves (performance-tracker backend/utils/metrics.js), scraped on the api
and cron jobs. They say what each process is *actually running*, read from the
tree it loaded at boot. That is true no matter who deployed or how.

`ci_*` and `deploy_*` come from the CI poller (scripts/ci-poller), scraped on
its own job. They say what the automation *did*.

Four things worth knowing before reading a panel here:

1. What the deployer claims and what is running are different facts. An aborted
   `git pull` leaves old code serving while every deploy step reports success;
   that failure has reached production in this project. So the version panels
   read the application's own metric and never the deploy's outcome, and
   `deploy_version_verified` is the poller comparing the two after the fact.

2. Nothing here can say *who* deployed, and no panel pretends to. The poller
   counts its own deploys; scripts/ship.sh, which is how a human deploys, emits
   no metrics at all. Restarts are not a usable proxy either: the api process
   has restarted hundreds of times without a deploy. The version table is
   therefore the honest answer to "what is out there", and the gap between it
   and deploy_total is where manual deploys live, uncounted. Fixing that means
   teaching ship.sh to report, not inferring it here.

3. `instance_id` is the application instance, never `instance`. Prometheus
   attaches its own `instance` naming the scrape target, and a metric declaring
   the same name is renamed to `exported_instance`, so a query against the plain
   name matches nothing, silently. Same reasoning as build-cronjobs.py note 1.

4. Counters are read through rate() or increase(). The poller is a PM2 fork and
   restarts with every deploy of itself; a panel summing raw counters would show
   the total drop to zero and call it a quiet period.

The watchdog panel, "CI alive", is the most important one on the page. If the
poller dies, CI stops and nothing else says so: no build starts, no status is
posted, and every pull request simply waits. Its threshold is documented at the
constant.
"""
import json

DS = {"type": "prometheus", "uid": "prometheus"}

panels = []
pid = 0

# The poller cycles every 60s. Five minutes is late enough that a slow cycle or a
# restart under PM2 does not trip it, and early enough that nobody spends an
# afternoon waiting for a build that was never going to start.
STALL_SECONDS = 300


def nid():
    global pid
    pid += 1
    return pid


def targets(*specs, instant=False, fmt=None):
    out = []
    for i, (expr, legend) in enumerate(specs):
        t = {"datasource": DS, "expr": expr, "legendFormat": legend,
             "refId": chr(65 + i), "editorMode": "code", "range": not instant,
             "instant": instant}
        if fmt:
            t["format"] = fmt
        out.append(t)
    return out


def row(title, y):
    return {"type": "row", "title": title, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "id": nid(), "collapsed": False, "panels": []}


def stat(title, expr, x, y, w=6, h=4, unit="short", desc="", thresholds=None,
         legend="", mappings=None, text_mode="auto"):
    return {
        "type": "stat", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": DS,
        "targets": targets((expr, legend)),
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "none", "textMode": text_mode,
                    "justifyMode": "auto", "orientation": "auto"},
        "fieldConfig": {"defaults": {"unit": unit, "mappings": mappings or [],
                                     "thresholds": thresholds or {"mode": "absolute",
                                                                  "steps": [{"color": "green", "value": None}]}},
                        "overrides": []},
    }


def ts(title, specs, x, y, w=12, h=8, unit="short", desc="", legend_calcs=None,
       minv=None, fill=10, stack=False):
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
                       "stacking": {"mode": "normal" if stack else "none"},
                       "scaleDistribution": {"type": "linear"}},
            "color": {"mode": "palette-classic"},
            "thresholds": {"mode": "absolute", "steps": [{"color": "green", "value": None}]},
        }, "overrides": []},
    }


def table(title, expr, x, y, w=12, h=8, desc="", hide=None, rename=None):
    """An instant query rendered as a table, which is how an info metric is read.

    app_build_info is a gauge fixed at 1 carrying its data in labels, so the
    value column is noise and the labels are the content.
    """
    transforms = [{"id": "organize", "options": {
        "excludeByName": {name: True for name in (hide or [])},
        "renameByName": rename or {},
    }}]
    return {
        "type": "table", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": DS,
        "targets": targets((expr, ""), instant=True, fmt="table"),
        "transformations": transforms,
        "options": {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"], "countRows": False}},
        "fieldConfig": {"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"},
                                                "inspect": False},
                                     "mappings": [],
                                     "thresholds": {"mode": "absolute",
                                                    "steps": [{"color": "green", "value": None}]}},
                        "overrides": []},
    }


def timeline(title, specs, x, y, w=24, h=8, desc=""):
    return {
        "type": "state-timeline", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": DS,
        "targets": targets(*specs),
        "options": {"showValue": "auto", "alignValue": "left", "mergeValues": True,
                    "rowHeight": 0.9, "legend": {"displayMode": "list", "placement": "bottom",
                                                 "showLegend": True},
                    "tooltip": {"mode": "single", "sort": "none"}},
        "fieldConfig": {"defaults": {"custom": {"lineWidth": 0, "fillOpacity": 80},
                                     "color": {"mode": "palette-classic"},
                                     "mappings": [],
                                     "thresholds": {"mode": "absolute",
                                                    "steps": [{"color": "green", "value": None}]}},
                        "overrides": []},
    }


AGE = "s"  # seconds, rendered by Grafana as a duration

# ── Right now ────────────────────────────────────────────────────
y = 0
panels.append(row("Right now", y))
y += 1

panels.append(stat(
    "CI alive", f"time() - ci_poller_last_poll_timestamp_seconds", 0, y, w=6, unit=AGE,
    desc="Age of the last completed poll cycle. The poller stamps this after every cycle "
         "whether the cycle threw or not, so it answers 'is the loop turning'. If the "
         f"poller dies, nothing else says so: no build starts and every pull request "
         f"simply waits. Red past {STALL_SECONDS}s.",
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "red", "value": STALL_SECONDS}]},
    legend="since last poll"))

panels.append(stat(
    "Hosts in agreement", "count(count by (commit) (app_build_info))", 6, y, w=6,
    desc="Distinct commits running across every scraped process. 1 means the server and "
         "the cron agree. Anything higher means a partial deploy, which used to be "
         "discoverable only by sshing into each host and running git rev-parse.",
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "red", "value": 2}]},
    mappings=[{"type": "value", "options": {"1": {"text": "agreed", "color": "green", "index": 0}}}],
    legend="distinct commits"))

panels.append(stat(
    "Deployed version verified", "min(deploy_version_verified)", 12, y, w=6,
    desc="The poller reading app_build_info back off each host after a deploy and "
         "comparing it with the SHA it shipped. 0 means every step reported success and "
         "the wrong code is serving, which is the failure the deploy scripts have always "
         "worked around blind. No data until the first automatic deploy runs.",
    thresholds={"mode": "absolute", "steps": [
        {"color": "red", "value": None}, {"color": "green", "value": 1}]},
    mappings=[{"type": "value", "options": {
        "0": {"text": "MISMATCH", "color": "red", "index": 0},
        "1": {"text": "verified", "color": "green", "index": 1}}}],
    legend="verified"))

panels.append(stat(
    "main last green", 'time() - ci_last_success_timestamp_seconds{ref_type="main"}',
    18, y, w=6, unit=AGE,
    desc="How long since main last passed validation. Grows without bound if main is red "
         "or if nothing is being merged; read it beside the runs panel below.",
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "orange", "value": 86400}]},
    legend="since last green main"))

# ── What is running ──────────────────────────────────────────────
y += 4
panels.append(row("What is running", y))
y += 1

panels.append(table(
    "Version per process", "app_build_info", 0, y, w=12, h=7,
    desc="Read from each process at boot, so it describes the code actually loaded rather "
         "than what a deploy believed it shipped. True regardless of who deployed or how. "
         "version is `git describe --tags --always`, which becomes meaningful once the "
         "deploy/prod/* tags the poller pushes exist.",
    hide=["Time", "Value", "__name__", "job", "instance"],
    rename={"service": "process", "commit": "commit", "branch": "branch",
            "version": "version", "instance_id": "pm2 instance"}))

panels.append(stat(
    "Uptime since last restart",
    "time() - app_start_timestamp_seconds", 12, y, w=12, h=7, unit=AGE,
    desc="Per process. A deploy restarts everything, so a young uptime across the board "
         "means something shipped recently. Not a deploy counter, though: this process has "
         "restarted hundreds of times without one, which is exactly why no panel here "
         "infers deploys from restarts.",
    legend="{{service}} {{instance_id}}", text_mode="value_and_name"))

y += 7
panels.append(timeline(
    "Version running over time",
    [("app_build_info", "{{service}} {{version}}")],
    0, y, w=24, h=7,
    desc="Each band is a version being served. A band ending and another starting is a "
         "deploy landing, whoever ran it. Bands that start at different times for server "
         "and cron are a deploy that reached one host before the other, which is normal "
         "for a few seconds and a problem if it persists."))

# ── Deploys ──────────────────────────────────────────────────────
y += 7
panels.append(row("Deploys", y))
y += 1

panels.append(stat(
    "Since last verified deploy", "time() - deploy_last_success_timestamp_seconds",
    0, y, w=6, unit=AGE,
    desc="Counts only deploys the poller ran and then verified. A deploy someone ran by "
         "hand with scripts/ship.sh does not appear here at all: it emits no metrics. The "
         "version table above is the honest answer to what is out there.",
    legend="since last"))

panels.append(stat(
    "Automatic deploys, 24h", "sum(increase(deploy_total{status=\"success\"}[24h]))",
    6, y, w=6,
    desc="Successful deploys by the poller in the last day. Compare with the version "
         "timeline: versions that changed without a bar here were deployed by hand.",
    legend="deploys"))

panels.append(stat(
    "Failed deploys, 24h", "sum(increase(deploy_total{status=\"failure\"}[24h]))",
    12, y, w=6,
    desc="A failed deploy has nowhere else to report. The commit status already says the "
         "code is good, and it is: what failed is putting it out there.",
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "red", "value": 1}]},
    legend="failures"))

panels.append(stat(
    "Poller deploys enabled", "count(deploy_total) > bool 0", 18, y, w=6,
    desc="Whether the poller has ever recorded a deploy. Off until CI_POLLER_DEPLOY=on, "
         "so 'no data' here and a moving version timeline above means every deploy is "
         "still being run by hand.",
    mappings=[{"type": "value", "options": {
        "0": {"text": "no deploys recorded", "color": "text", "index": 0},
        "1": {"text": "active", "color": "green", "index": 1}}}],
    legend="active"))

y += 4
panels.append(ts(
    "Deploy outcomes",
    [("sum by (status) (increase(deploy_total[1h]))", "{{status}}")],
    0, y, w=12, unit="short", minv=0, stack=True,
    desc="Automatic deploys only, by outcome, per hour. The stage label on a failure says "
         "how far it got: lock, server, cron or verify. verify is the interesting one, "
         "because it means every step succeeded and the wrong code is serving.",
    legend_calcs=["sum"]))

panels.append(ts(
    "Version verified per host",
    [("deploy_version_verified", "{{host}}")],
    12, y, w=12, unit="short", minv=0, fill=30,
    desc="1 when the host runs what the deploy shipped, 0 when it does not. A dip to 0 is "
         "the aborted-pull failure caught in the act.",
    legend_calcs=["min", "lastNotNull"]))

# ── CI ───────────────────────────────────────────────────────────
y += 8
panels.append(row("CI", y))
y += 1

panels.append(ts(
    "Build outcomes",
    [("sum by (result) (increase(ci_runs_total[1h]))", "{{result}}")],
    0, y, w=12, unit="short", minv=0, stack=True,
    desc="Builds that reached a verdict, per hour. 'retry' is not a third kind of failure: "
         "it means AWS reclaimed the task or it never started, so the branch was never "
         "judged and nothing was reported to the pull request.",
    legend_calcs=["sum"]))

panels.append(ts(
    "Build duration p50 / p95",
    [("histogram_quantile(0.50, sum by (le) (rate(ci_run_duration_seconds_bucket[6h])))", "p50"),
     ("histogram_quantile(0.95, sum by (le) (rate(ci_run_duration_seconds_bucket[6h])))", "p95")],
    12, y, w=12, unit=AGE, minv=0,
    desc="A build runs about six minutes, most of it a cold `npm ci` over 2.5 GB of "
         "packages, because a Fargate task has no persistent disk to keep node_modules "
         "warm. If this climbs, baking the dependencies into an ECR image is the lever.",
    legend_calcs=["mean", "max"]))

y += 8
panels.append(stat(
    "Queue depth", "ci_queue_depth", 0, y, w=6,
    desc="Candidates the last cycle decided to build. Sustained above a few means builds "
         "are arriving faster than they finish, which they cannot: the poller runs one at "
         "a time because its checkout is shared.",
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "orange", "value": 5}]},
    legend="queued"))

panels.append(stat(
    "Building now", "ci_runs_in_progress", 6, y, w=6,
    desc="0 or 1 by design.", legend="in progress"))

panels.append(stat(
    "Spot reclaims, 24h", "increase(ci_spot_interruptions_total[24h])", 12, y, w=6,
    desc="Builds AWS took back before they finished. Each one costs a retry, not a red "
         "status. Expected occasionally: Spot carries the work at roughly a third of the "
         "price and the first build ever run here was reclaimed. Sustained and high is the "
         "signal to shift weight toward on-demand in devops/ci/apply.sh.",
    thresholds={"mode": "absolute", "steps": [
        {"color": "green", "value": None}, {"color": "orange", "value": 5}]},
    legend="reclaims"))

panels.append(stat(
    "main last red", 'time() - ci_last_failure_timestamp_seconds{ref_type="main"}',
    18, y, w=6, unit=AGE,
    desc="How long since main last failed. Large is good.",
    legend="since last red main"))

dashboard = {
    "uid": "pt-ci-deploys",
    "title": "CI and Deploys",
    "description": "What the CI poller is doing and what each process is actually running. "
                   "Version facts come from the applications themselves; deploy facts come "
                   "from the poller and cover automatic deploys only.",
    "tags": ["performance-tracker", "ci", "observability"],
    "timezone": "browser",
    "editable": True,
    "schemaVersion": 39,
    "version": 1,
    "refresh": "30s",
    "time": {"from": "now-24h", "to": "now"},
    "panels": panels,
    "templating": {"list": []},
    "annotations": {"list": []},
}

with open("grafana/dashboards/ci-deploys.json", "w") as f:
    json.dump(dashboard, f, indent=2)
    f.write("\n")

print("wrote grafana/dashboards/ci-deploys.json")
print("panels:", len([p for p in panels if p["type"] != "row"]),
      "+", len([p for p in panels if p["type"] == "row"]), "rows")
