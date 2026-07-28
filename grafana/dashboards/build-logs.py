"""Generate the Logs dashboard JSON.

Run from the repo root:  python3 grafana/dashboards/build-logs.py

Shaped after grafana.com dashboard 13639 ("Logs / App"), which contributed two
good ideas: a volume-over-time graph and a free-text search box. Everything else
differs, because 13639 queries {job="$app"} with no level filter at all. Measured
here, that is 134k lines/hour against 3.9k on stderr — 97% informational noise
drowning the errors. It also pipes through `| logfmt`, which extracts nothing
from logs that are plain console.* text.
"""
import json

DS = {"type": "loki", "uid": "loki"}

# Errors carry a [module] prefix: `[hubspot] POST ... responded 429`. Extracting
# it is the closest thing to Sentry's issue grouping available here — it answers
# "what is broken right now" without fingerprinting exceptions.
# Backtick-quoted so the pattern needs no escaped quotes inside LogQL.
MODULE_RE = r"| regexp `^\[(?P<module>[^\]]+)\]`"

panels = []
pid = 0


def nid():
    global pid
    pid += 1
    return pid


def targets(*exprs):
    return [{"datasource": DS, "expr": e, "refId": chr(65 + i), "queryType": "range",
             "editorMode": "code"}
            for i, e in enumerate(exprs)]


def row(title, y):
    return {"type": "row", "title": title, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "id": nid(), "collapsed": False, "panels": []}


def ts(title, exprs, x, y, w, h, desc="", unit="short", stack=False):
    return {
        "type": "timeseries", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": DS,
        "targets": targets(*exprs),
        "options": {"legend": {"displayMode": "table", "placement": "bottom",
                               "showLegend": True, "calcs": ["sum", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "fieldConfig": {"defaults": {
            "unit": unit, "min": 0,
            "custom": {"drawStyle": "bars", "lineWidth": 1, "fillOpacity": 60,
                       "showPoints": "never",
                       "stacking": {"mode": "normal" if stack else "none"}},
            "color": {"mode": "palette-classic"},
        }, "overrides": []},
    }


# ── Overview ─────────────────────────────────────────────────────
y = 0
panels.append(row("Overview", y))
y += 1
panels.append(ts(
    "Error volume over time",
    ['sum by (job) (count_over_time({job=~"$job", stream="err"} |= "$search" [$__interval]))'],
    0, y, 12, 8, stack=True,
    desc="Everything on stderr. This is the panel that answers \"did something start "
         "failing at 3am\" — the log list alone cannot show that."))
panels.append(ts(
    "stdout lines matching error/warn",
    ['sum by (job) (count_over_time({job=~"$job", stream="out"} |= "$search" '
     '|~ "(?i)(error|warn)" [$__interval]))'],
    12, y, 12, 8, stack=True,
    desc="pino writes every level to stdout, so logger.error lands here rather than on "
         "stderr. Matching on text is the only way to find it until the backend stops "
         "logging through console.*."))
y += 8
panels.append({
    "type": "barchart", "title": "Top failing modules", "id": nid(),
    "description": "Errors grouped by their [module] prefix over the dashboard's time range. "
                   "Not exception fingerprinting — but it answers what is failing most, "
                   "which is what you usually open a log dashboard to find out.",
    "gridPos": {"h": 9, "w": 24, "x": 0, "y": y}, "datasource": DS,
    "targets": targets(
        'topk(15, sum by (module) (count_over_time({job=~"$job", stream="err"} '
        '|= "$search" ' + MODULE_RE + ' [$__range])))'),
    "options": {"orientation": "horizontal", "showValue": "always",
                "legend": {"showLegend": False},
                "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
    "fieldConfig": {"defaults": {"unit": "short", "color": {"mode": "palette-classic"}},
                    "overrides": []},
})

# ── Logs ─────────────────────────────────────────────────────────
y += 9
panels.append(row("Logs", y))
y += 1
panels.append({
    "type": "logs", "title": "Log stream", "id": nid(),
    "description": "Filtered by the variables above. Use the search box for free text; it is "
                   "a substring match, not a regex.",
    "gridPos": {"h": 18, "w": 24, "x": 0, "y": y}, "datasource": DS,
    "targets": targets('{job=~"$job", stream=~"$stream"} |= "$search"'),
    "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                "enableLogDetails": True, "dedupStrategy": "none",
                "prettifyLogMessage": False},
})

dashboard = {
    "uid": "pt-logs",
    "title": "Performance Tracker Logs",
    "description": "Warning and error logs from the application EC2, shipped by Grafana Alloy. "
                   "Retention is 30 days and these lines carry PII.",
    "tags": ["performance-tracker", "observability", "logs"],
    "timezone": "browser",
    "editable": True,
    "schemaVersion": 39,
    "version": 1,
    "refresh": "1m",
    "time": {"from": "now-6h", "to": "now"},
    "panels": panels,
    "templating": {"list": [
        {
            "name": "job", "label": "Job", "type": "query", "datasource": DS,
            "query": {"label": "job", "type": "1"},
            # The regex is not cosmetic: throwaway push tests during the rollout left
            # `ac`, `pos` and `smoke` in the job label. Loki's delete API defers the
            # actual removal, so without this filter they sit in the dropdown for as
            # long as their chunks survive.
            "regex": "^(server|cron)$",
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]}, "refresh": 1,
        },
        {
            "name": "stream", "label": "Stream", "type": "custom",
            "query": "err,out",
            "options": [
                {"text": "err", "value": "err", "selected": True},
                {"text": "out", "value": "out", "selected": False},
            ],
            # Defaults to stderr alone: including stdout multiplies the volume ~35x
            # and buries the errors.
            "current": {"text": ["err"], "value": ["err"]},
            "multi": True, "includeAll": True, "allValue": ".*",
        },
        {
            "name": "search", "label": "Search", "type": "textbox",
            "query": "", "current": {"text": "", "value": ""},
        },
    ]},
    "annotations": {"list": []},
}

with open("grafana/dashboards/logs.json", "w") as f:
    json.dump(dashboard, f, indent=2)
    f.write("\n")

print("wrote grafana/dashboards/logs.json")
print("panels:", len([p for p in panels if p["type"] != "row"]),
      "+", len([p for p in panels if p["type"] == "row"]), "rows")
