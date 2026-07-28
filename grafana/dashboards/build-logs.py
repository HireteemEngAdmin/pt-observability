"""Generate the Logs dashboard JSON.

Run from the repo root:  python3 grafana/dashboards/build-logs.py

Shaped after grafana.com dashboard 13639 ("Logs / App"), which contributed two
good ideas: a volume-over-time graph and a free-text search box.

Rewritten once the backend finished migrating off console.*. The first version
read stderr and pulled a [module] prefix out of the message text, because that
was all the logs carried. Both premises are now wrong: pino writes every level
to stdout, so stderr holds no application errors at all, and nothing emits a
[module] prefix any more. Measured before this rewrite: 69 error lines an hour
on stdout, and 0 on stderr, against a panel that only read stderr.

So the queries parse the JSON and read the fields the logger actually sets.
"""
import json

DS = {"type": "loki", "uid": "loki"}

# Every pino line carries service and environment from the logger's `base`, so
# this is the cheapest way to tell our own structured output from anything else
# sharing the same stream: Node's own warnings, and whatever a migration prints
# at boot.
PINO_MARK = r'`"service":"performance-tracker"`'

# `| json` on a line that is not JSON does not drop it, it tags it with
# __error__="JSONParserErr" and carries on. Filtering on that keeps a stray
# unstructured line from being counted as a module named "".
PARSED = r'| json | __error__=""'


panels = []
pid = 0


def nid():
    global pid
    pid += 1
    return pid


def targets(*exprs, instant=False):
    return [{"datasource": DS, "expr": e, "refId": chr(65 + i),
             "queryType": "instant" if instant else "range",
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
    "Errors and warnings over time",
    ['sum by (level) (count_over_time({job=~"$job"} |= "$search" ' + PARSED +
     ' | level=~"error|warn" [$__interval]))'],
    0, y, 12, 8, stack=True,
    desc="Read from the level field, not from which stream the line arrived on. "
         "This is the panel that answers \"did something start failing at 3am\", "
         "which the log list alone cannot show."))
panels.append(ts(
    "Unstructured lines",
    ['sum by (stream) (count_over_time({job=~"$job"} != ' + PINO_MARK + ' [$__interval]))'],
    12, y, 12, 8, stack=True,
    desc="Lines that did not come from the logger. This is the migration tracker: the "
         "backend is at zero console.* calls, so this should stay flat at whatever "
         "Node itself emits. Two known sources are legitimate, both at boot: Node's own "
         "warnings, and the console output inside migrations/, which run in the deployed "
         "process and are deliberately outside the console ban. A sustained rise here "
         "during normal traffic means something went back to writing raw."))
y += 8
panels.append({
    "type": "bargauge", "title": "Top failing modules", "id": nid(),
    "description": "Errors grouped by the logger's module field over the dashboard's time "
                   "range. Not exception fingerprinting, but it answers what is failing "
                   "most, which is what you usually open a log dashboard to find out. "
                   "A line with no module means the logger was used without a binding.",
    "gridPos": {"h": 12, "w": 24, "x": 0, "y": y}, "datasource": DS,
    # instant, not range. A range query evaluates the topk at every step, so the
    # panel receives 10 series times ~45 steps and renders 450 unreadable slivers
    # instead of 10 bars. Top-N over a window is a single evaluation.
    #
    # label_replace still names the empty bucket. It should now be rare rather than
    # the norm: it means a childLogger call with no module binding, which is worth
    # seeing rather than hiding.
    "targets": targets(
        'label_replace('
        'topk(10, sum by (module) (count_over_time({job=~"$job"} '
        '|= "$search" ' + PARSED + ' | level="error" [$__range])))'
        ', "module", "(no module)", "module", "^$")',
        instant=True),
    # Loki answers with one frame per series, and the numeric field is called
    # "Value" in every one of them — the module name exists only in the field
    # labels. The panel cannot read labels on its own: it collapses the frames
    # and draws a single bar named "Value #A".
    #
    # labelsToFields promotes the label to a real field, merge folds the ten
    # frames into one table, and values:true then draws a bar per row using the
    # module column as the name. A displayName template does not work here —
    # ${__field.labels.module} is resolved before the frames are merged.
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
})

# ── Logs ─────────────────────────────────────────────────────────
y += 12
panels.append(row("Logs", y))
y += 1
panels.append({
    "type": "logs", "title": "Log stream", "id": nid(),
    "description": "Filtered by the variables above. Use the search box for free text; it is "
                   "a substring match, not a regex.",
    "gridPos": {"h": 18, "w": 24, "x": 0, "y": y}, "datasource": DS,
    # Unstructured lines have no level field, so `| json | level=~"$level"` would
    # hide them entirely and the panel would stop showing Node's own crashes. The
    # or-clause keeps anything that failed to parse.
    "targets": targets(
        '{job=~"$job", stream=~"$stream"} |= "$search" | json '
        '| level=~"$level" or __error__="JSONParserErr"'),
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
            # ".+" and not ".*". Loki rejects a selector whose every matcher can
            # match the empty string, and both of the labels used here are
            # variables with an All option. With All selected on both, ".*"
            # produced {job=~".*", stream=~".*"} and Loki refused the whole
            # query, so the panel showed nothing at all rather than showing
            # everything. ".+" still matches every job, since none is unlabelled.
            "multi": True, "includeAll": True, "allValue": ".+",
            "current": {"text": ["All"], "value": ["$__all"]}, "refresh": 1,
        },
        {
            "name": "stream", "label": "Stream", "type": "custom",
            "query": "err,out",
            "options": [
                {"text": "err", "value": "err", "selected": False},
                {"text": "out", "value": "out", "selected": False},
            ],
            # Used to default to stderr alone, when stdout was 35x the volume and all
            # of it noise. That is inverted now: pino writes every level to stdout, so
            # stderr holds no application errors and the old default showed an empty
            # dashboard. Both streams by default, and the level filter below is what
            # cuts the volume.
            "current": {"text": ["All"], "value": ["$__all"]},
            # See the note on the job variable: this one is in the selector too.
            "multi": True, "includeAll": True, "allValue": ".+",
        },
        {
            "name": "level", "label": "Level", "type": "custom",
            "query": "error,warn,info,debug",
            "options": [
                {"text": "error", "value": "error", "selected": True},
                {"text": "warn", "value": "warn", "selected": True},
                {"text": "info", "value": "info", "selected": False},
                {"text": "debug", "value": "debug", "selected": False},
            ],
            # error and warn by default. info is roughly 60x the volume and is what
            # the old dashboard was avoiding by reading stderr only.
            "current": {"text": ["error", "warn"], "value": ["error", "warn"]},
            "multi": True, "includeAll": True, "allValue": ".*",
        },
        {
            "name": "search", "label": "Search", "type": "textbox",
            "query": "", "current": {"text": "", "value": ""},
        },
    ]},
    "annotations": {"list": []},
}

# Loki requires at least one matcher in a stream selector that cannot match the
# empty string. A dashboard whose selector is built entirely from All-able
# variables violates that only when someone selects All, so it cannot be caught
# by reading the JSON. Checking the generated values here is what makes it
# visible at build time.
EMPTY_COMPATIBLE = {".*", "", ".*?"}


def assert_selectors_cannot_be_empty(dash):
    by_name = {v["name"]: v for v in dash["templating"]["list"]}
    for panel in dash["panels"]:
        for target in panel.get("targets", []):
            expr = target.get("expr", "")
            if not expr.startswith("{"):
                continue
            selector = expr[1:expr.index("}")]
            resolved = []
            for part in selector.split(","):
                value = part.split("=", 1)[1].strip().strip('"~= ')
                if value.startswith("$"):
                    var = by_name.get(value[1:])
                    value = var.get("allValue", "") if var else ""
                resolved.append(value)
            if all(v in EMPTY_COMPATIBLE for v in resolved):
                raise SystemExit(
                    f"panel {panel.get('title')!r}: every matcher in its stream "
                    f"selector can match empty ({resolved}). Loki rejects the "
                    "whole query and the panel renders blank."
                )


assert_selectors_cannot_be_empty(dashboard)

with open("grafana/dashboards/logs.json", "w") as f:
    json.dump(dashboard, f, indent=2)
    f.write("\n")

print("wrote grafana/dashboards/logs.json")
print("panels:", len([p for p in panels if p["type"] != "row"]),
      "+", len([p for p in panels if p["type"] == "row"]), "rows")
