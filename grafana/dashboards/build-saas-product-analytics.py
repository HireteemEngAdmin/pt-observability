"""Generate the SaaS Product Analytics dashboard.

Run from the repo root:  python3 grafana/dashboards/build-saas-product-analytics.py

This is the AGGREGATE dashboard. Its companion,
build-saas-product-analytics-users.py (uid pt-saas-analytics-users), holds the
two panels that carry names and email addresses. The split exists because
Grafana grants access per dashboard and not per panel, so putting a user list in
its own dashboard is the only way to restrict who can read it. Nothing in this
file identifies an individual: every panel here is a count, a rate or a
per-tenant aggregate.

Product-side view of the Performance Tracker: who is active, who logs in, which
features get used, and how the tenants compare. Three datasources:

- Prometheus, for the saas_* metrics being added in performance-tracker
  (backend/utils/, branch feat/saas-product-metrics) and the
  http_request_duration_seconds histogram the api job already exposes.
- Loki, for the pino logs.
- PostgreSQL (uid postgres), for anything that needs row-level history: the
  per-company table, retention, and activity that predates the metrics.
  Provisioned in grafana/provisioning/datasources/postgres.yml, reaching RDS
  through an SSH tunnel because the instance sits in a private subnet. The role
  is granted SELECT on four tables and no more (sql/grafana-readonly-role.sql):
  action_logs, "Users", "Tenants", companies. Every query here stays inside them.

Four constraints shaped this dashboard more than anything else.

1. The three datasources answer different questions and are not
   interchangeable. Prometheus holds aggregates with no user dimension, because a
   user id as a metric label is unbounded cardinality: no user or company
   identifier appears in a Prometheus query anywhere in this file, and
   company_slug is the single exception, safe at cardinality 2. Postgres holds
   the row-level history back to 2024-03-05, which is why retention is SQL and
   not a PromQL expression. Loki holds the structured events, where userId and
   companyId are body fields and so are filterable.

2. DAU/WAU/MAU are gauges, not counters. The backend computes the distinct
   counts; the dashboard reads them. So: no rate() on them, and no summing daily
   values to reach a weekly one. sum(DAU over 7 days) counts a user who logged
   in every day seven times, which is not what WAU means. sum() across
   company_slug IS safe, because a user belongs to exactly one tenant, so the
   per-company distinct sets do not overlap.

3. The weekly cycle is real and large: ~1175 unique users on a weekday against
   60 to 160 at the weekend. That is an order of magnitude, so any panel that
   plots DAU as one flat line makes every Sunday look like an outage. The DAU
   panel splits the series by day_of_week() so weekdays and weekends are
   different colours and the dip is self-explaining.

4. Counters get rate() or increase(), never a raw value. pm2 reload resets the
   process, so a raw counter reading is "since the last deploy", which is a
   different window on every panel and on every refresh.

Loki notes, since the log panels are easy to get silently wrong:

- Prometheus and Loki disagree on what the processes are called. Prometheus
  scrapes jobs `api` and `cron` (prometheus/prometheus.yml); Loki receives jobs
  `server` and `cron`, because Alloy names streams after the PM2 process. A
  single $job variable would therefore match nothing in Loki for the API, and it
  would do so silently, since an empty result is a valid query. Hence a separate
  $loki_job.
- Filtering order is selector, then line filter, then `| json`. A line filter is
  a substring scan over compressed chunks; a JSON parse allocates per line.
  Doing `| json` first and filtering after works and costs an order of magnitude
  more.
- The logger's level is `info` in production (backend/utils/logger.js sets
  `defaultLevel`), so `level=debug` is never emitted there. Measured over one
  hour on the live stack: 3327 info, 581 warn, 28 error, zero debug. Anything
  logged at debug, successful request traces among them, is not in Loki at all,
  so the log panels here are the warn/error/info surface only.
- `userId` and `companyId` are body fields, never stream labels. They are
  reachable only after `| json`, which is why the company and user filters in
  this dashboard are Loki-side only.

Two Prometheus label rules this file obeys, both learned the hard way in this
repo:

- Never `instance` as an application label. Prometheus attaches its own
  `instance` naming the scrape target, so an application label of that name is
  silently renamed to `exported_instance` and a panel filtering on `instance`
  matches nothing while looking correct. The same collision already bit `job`
  here.
- Never a raw counter value, always rate() or increase().

SQL notes, since these queries touch a 309 MB table:

- action_logs is 2.1M rows and 309 MB, from 2024-03-05. The only indexes on
  created_at are partial, restricted to method IN ('ACTIVATED','DEACTIVATED'),
  so they do not serve activity queries: assume a filtered scan. Every query
  that touches action_logs is therefore bounded by the dashboard's time range
  through $__timeFilter. The queries that read only "Users", "Tenants" and
  companies are not bounded, and do not need to be: those are 5384, 2047 and 2
  rows.
- "Active" here means logged in or performed a mutating action. action_logs is
  written for authenticated POST/PATCH/PUT/DELETE and on successful login with
  route='/login'. Successful reads are recorded nowhere, so a user who only ever
  reads is invisible to every activity panel on this dashboard. Each such panel
  description says so, because it is the kind of definition that gets forgotten
  and then quoted.
- Test accounts excluded everywhere: email LIKE 'mocktm_%' (5 rows) and email
  LIKE '%demo%' (9 rows), and nothing else. In particular @hireteem.com and
  @getreach.com are NOT excluded: those are the Team Members, who are the real
  users. Note the `_` in 'mocktm_%' is a LIKE wildcard rather than a literal, so
  the pattern is "mocktm" plus any character plus anything. It is left unescaped
  on purpose, so the exclusion reproduces exactly the 5 rows that were counted.
- Identifier quoting is not decoration. Sequelize created "Users" and "Tenants"
  with capitals and camelCase columns ("createdAt", "TenantId", "CompanyId"),
  while action_logs and companies are lower_snake. Postgres folds unquoted
  identifiers to lower case, so the capitalised ones need double quotes or the
  query fails with "relation users does not exist".
"""
import json

PROM = {"type": "prometheus", "uid": "prometheus"}
LOKI = {"type": "loki", "uid": "loki"}
PG = {"type": "postgres", "uid": "postgres"}

# The only two test-account patterns there are. Not @hireteem.com or
# @getreach.com: those addresses belong to the Team Members, who are the largest
# group of real users, and excluding them would silently delete most of the
# product's activity from every panel.
NO_TEST = """u.email NOT LIKE 'mocktm_%'
     AND u.email NOT LIKE '%demo%'"""

# action_logs records authenticated mutations and successful logins. Reads leave
# no row, so this sentence belongs in the description of every panel built on it.
ACTIVITY_DEF = ("\"Active\" means logged in (action_logs.route = '/login') or performed an "
                "authenticated POST/PATCH/PUT/DELETE. Successful reads are recorded "
                "nowhere, so a user who only reads never appears here.")

SCAN_NOTE = ("Bounded by the dashboard time range. action_logs is 2.1M rows and 309 MB and "
             "its only created_at indexes are partial (method IN "
             "('ACTIVATED','DEACTIVATED')), so this is a filtered scan: widening the range "
             "costs real time.")

# environment is a label on every saas_* metric. company_slug is only on the
# aggregate gauges, so it is spliced in per metric instead of living in one shared
# selector: writing company_slug=~"teem" against a counter that does not carry the
# label returns nothing, which reads as "not deployed yet" rather than as a bug.
ENV = 'environment=~"$environment"'
GAUGE = ENV + ', company_slug=~"$company_slug"'

# Weekday / weekend classification. day_of_week() with no argument evaluates at
# the step timestamp and returns 0=Sunday .. 6=Saturday, in UTC. The dashboard
# renders in browser time, so for a UTC-3 viewer the boundary lands at 21:00
# local and the last three hours of a Friday evening are classified as Saturday.
# That is a known and acceptable seam: it shifts the colour change by three
# hours, it does not change the shape of the curve.
WEEKDAY = "(day_of_week() >= 1 and day_of_week() <= 5)"
WEEKEND = "(day_of_week() == 0 or day_of_week() == 6)"

# Loki: cheap stream selector, then a substring line filter, then the parser.
# `service` and `environment` are on every pino line via the logger's `base`, so
# this substring is the cheapest way to separate our structured output from
# Node's own warnings and from whatever the boot migrations print.
PINO = r'|= `"service":"performance-tracker"`'
# `| json` does not drop a line that is not JSON, it tags it __error__="JSONParserErr"
# and carries on. Dropping those keeps a stray unstructured line from being
# counted as an event named "".
PARSED = r'| json | __error__=""'

# Every field below is optional on a given line, and a label filter against a
# missing field compares against the empty string. Verified on the live stack:
# this whole chain with every variable at .* still returns lines that carry none
# of the fields, so the filters narrow when set and are transparent when not.
LOG_FIELDS = (' | environment=~"$environment" | level=~"$level"'
              ' | event=~"$event" | feature=~"$feature" | result=~"$result"'
              ' | userRole=~"$user_role" | companyId=~"$company_id"'
              ' | userId=~"$user_id"')

panels = []
pid = 0


def nid():
    global pid
    pid += 1
    return pid


def targets(*specs, instant=False, datasource=PROM):
    """One target per (expr, legend) pair. Legend may be None for Loki log panels."""
    out = []
    for i, spec in enumerate(specs):
        expr, legend = spec if isinstance(spec, tuple) else (spec, None)
        t = {"datasource": datasource, "expr": expr, "refId": chr(65 + i),
             "editorMode": "code", "range": not instant, "instant": instant}
        if legend:
            t["legendFormat"] = legend
        if datasource is LOKI:
            t.pop("range")
            t.pop("instant")
            t["queryType"] = "instant" if instant else "range"
        out.append(t)
    return out


def sql_targets(query, fmt="table"):
    """Postgres target. rawSql plus rawQuery:true is what makes Grafana run the SQL
    verbatim instead of trying to rebuild it in the visual query builder, which
    silently drops CTEs. `format` picks the frame shape: "table" for the tables,
    "time_series" for anything with a column aliased `time`."""
    return [{"datasource": PG, "refId": "A", "rawQuery": True, "rawSql": query,
             "format": fmt, "editorMode": "code"}]


def row(title, y):
    return {"type": "row", "title": title, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "id": nid(), "collapsed": False, "panels": []}


def stat(title, expr, x, y, w=3, h=4, unit="short", desc="", steps=None, legend="",
         dec=None, mappings=None):
    return {
        "type": "stat", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": PROM,
        "targets": targets((expr, legend), instant=True),
        "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                    "colorMode": "value", "graphMode": "none", "textMode": "auto",
                    "justifyMode": "auto", "orientation": "auto"},
        "fieldConfig": {"defaults": {
            "unit": unit, "decimals": dec, "mappings": mappings or [],
            "thresholds": {"mode": "absolute",
                           "steps": steps or [{"color": "text", "value": None}]},
        }, "overrides": []},
    }


def ts(title, specs, x, y, w=12, h=8, unit="short", desc="", stack=False, bars=False,
       span_nulls=True, dec=None, minv=0, datasource=PROM):
    return {
        "type": "timeseries", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": datasource,
        "targets": targets(*specs, datasource=datasource),
        "options": {"legend": {"displayMode": "table", "placement": "bottom",
                               "showLegend": True, "calcs": ["mean", "max", "lastNotNull"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "fieldConfig": {"defaults": {
            "unit": unit, "min": minv, "decimals": dec,
            "custom": {"drawStyle": "bars" if bars else "line", "lineWidth": 1,
                       "fillOpacity": 60 if bars else 20, "showPoints": "never",
                       # spanNulls off wherever a gap is the message rather than a
                       # missing scrape: the weekday and weekend series are each
                       # absent for the other's steps, and joining across the gap
                       # would draw a line through days the series does not cover.
                       "spanNulls": span_nulls,
                       "stacking": {"mode": "normal" if stack else "none"}},
            "color": {"mode": "palette-classic"},
        }, "overrides": []},
    }


def ranked(title, expr, x, y, w=12, h=8, unit="short", desc=""):
    """Top-N bar gauge over the dashboard's time range.

    Instant, not range: a range query re-evaluates the topk at every step, so the
    panel receives N series times ~45 steps and renders hundreds of unreadable
    slivers instead of N bars.

    Prometheus answers with one frame per series and names the numeric field
    "Value" in all of them, with the grouping label only in the frame's labels.
    labelsToFields promotes it to a real field, merge folds the frames into one
    table, and values:true then draws one bar per row. Without this the panel
    collapses into a single unlabelled bar named "Value #A".
    """
    return {
        "type": "bargauge", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": PROM,
        "targets": targets(expr, instant=True),
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
        "targets": targets(expr, datasource=LOKI),
        "options": {"showTime": True, "wrapLogMessage": True, "sortOrder": "Descending",
                    "enableLogDetails": True, "dedupStrategy": "none",
                    "prettifyLogMessage": False},
    }


def table(title, query, x, y, w=24, h=10, desc="", overrides=None):
    return {
        "type": "table", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": PG,
        "targets": sql_targets(query, fmt="table"),
        "options": {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"], "countRows": False,
                               "fields": ""}},
        "fieldConfig": {"defaults": {
            "custom": {"align": "auto", "cellOptions": {"type": "auto"},
                       "inspect": False, "filterable": True},
        }, "overrides": overrides or []},
    }


def sql_ts(title, query, x, y, w=12, h=8, unit="short", desc="", stack=False, bars=False,
           span_nulls=True, dec=None):
    """Timeseries from SQL. Grafana's time_series format needs one column aliased
    `time`; every other column becomes a series named after its alias, which is why
    the SQL below aliases with the legend text it should display."""
    return {
        "type": "timeseries", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": PG,
        "targets": sql_targets(query, fmt="time_series"),
        "options": {"legend": {"displayMode": "table", "placement": "bottom",
                               "showLegend": True, "calcs": ["mean", "max", "lastNotNull"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}},
        "fieldConfig": {"defaults": {
            "unit": unit, "min": 0, "decimals": dec,
            "custom": {"drawStyle": "bars" if bars else "line", "lineWidth": 1,
                       "fillOpacity": 60 if bars else 20, "showPoints": "never",
                       "spanNulls": span_nulls,
                       "stacking": {"mode": "normal" if stack else "none"}},
            "color": {"mode": "palette-classic"},
        }, "overrides": []},
    }


def note(title, x, y, w, h, content):
    return {
        "type": "text", "title": title, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "options": {"mode": "markdown", "content": content},
        "transparent": False,
    }


# The SQL panels are written and reviewable but have never been executed: the
# tunnel is not up yet. Checked while building this: grafana-pg-tunnel.service
# reports inactive, 172.17.0.1:5432 is closed, and secrets/grafana-postgres.env
# does not exist on the host. So the SQL is verified by reading the Sequelize
# models for the schema and by reasoning about the plan, not by running it.
PG_NOT_LIVE = (
    "**Not yet executed.** This query has been written and reviewed but never run: the "
    "PostgreSQL datasource is provisioned (uid `postgres`) and the tunnel it needs "
    "(`grafana-pg-tunnel.service`) is not up yet, so 172.17.0.1:5432 is closed. Column and "
    "table names come from the Sequelize models in performance-tracker "
    "`backend/models/`, not from the server. Expect to correct something the first time it "
    "runs."
)


# ── Overview ─────────────────────────────────────────────────────
y = 0
panels.append(row("Overview", y))
y += 1
panels.append(stat(
    "Daily active users", f'sum(saas_active_users{{window="1d", {GAUGE}}})', 0, y,
    desc="Distinct users the backend counted as active in the 1d window, summed across "
         "tenants. Safe to sum: a user belongs to exactly one tenant, so the per-tenant "
         "distinct sets do not overlap. This is a gauge, so the panel reads it as-is: no "
         "rate(), and no adding up days. Expect ~1175 on a weekday and 60-160 at the "
         "weekend."))
panels.append(stat(
    "Weekly active users", f'sum(saas_active_users{{window="7d", {GAUGE}}})', 3, y,
    desc="Distinct users active in the 7d window, as computed in the backend. It is NOT "
         "the sum of seven daily figures: that would count a user who logs in every day "
         "seven times."))
panels.append(stat(
    "Monthly active users", f'sum(saas_active_users{{window="30d", {GAUGE}}})', 6, y,
    desc="Distinct users active in the 30d window, as computed in the backend. Same "
         "reasoning as WAU: a distinct count, not an accumulation."))
panels.append(stat(
    "Stickiness (DAU/MAU)",
    f'100 * sum(saas_active_users{{window="1d", {GAUGE}}}) '
    f'/ sum(saas_active_users{{window="30d", {GAUGE}}})',
    9, y, unit="percent", dec=1,
    desc="DAU divided by MAU, as a percentage: of the users who showed up this month, the "
         "share who showed up today. Inherits the weekly cycle from its numerator, so a "
         "weekend reading is not comparable with a weekday one.",
    steps=[{"color": "text", "value": None}]))
panels.append(stat(
    "Registered users",
    f'sum(saas_users_total{{{GAUGE}, role=~"$user_role", status=~"$user_status"}})',
    12, y,
    desc="Rows in the users table matching the role and status filters. A gauge of "
         "current state, not a signup rate: it says how many accounts exist, not how "
         "many were created in the selected range."))
panels.append(stat(
    "Companies", f'max(saas_companies_total{{{ENV}}})', 15, y,
    desc="Tenant companies that exist. max() and not sum(): the gauge is exposed by both "
         "the api and cron processes reading the same database, so sum() would double it."))
panels.append(stat(
    "Login success rate (range)",
    f'(sum(increase(saas_login_attempts_total{{result="success", {ENV}}}[$__range])) '
    f'or vector(0)) / sum(increase(saas_login_attempts_total{{{ENV}}}[$__range]))',
    18, y, unit="percentunit", dec=3,
    desc="Successful login attempts over all attempts, across the selected range. From "
         "increase() rather than the raw counters, because pm2 reload restarts the "
         "process and zeroes them, which would make the raw ratio mean \"since the last "
         "deploy\". or vector(0) keeps a range with no successes rendering as 0 instead "
         "of No data, which is indistinguishable from a broken scrape.",
    steps=[{"color": "red", "value": None}, {"color": "orange", "value": 0.9},
           {"color": "green", "value": 0.97}]))
panels.append(stat(
    "API 5xx rate (range)",
    '(sum(increase(http_request_duration_seconds_count{job="api", status_code=~"5.."}'
    '[$__range])) or vector(0)) '
    '/ sum(increase(http_request_duration_seconds_count{job="api"}[$__range]))',
    21, y, unit="percentunit", dec=4,
    desc="Share of API requests answered with 5xx over the selected range. This one is "
         "not a saas_* metric: it comes from the http_request_duration_seconds histogram "
         "the api job has exposed since before this dashboard, so it has real history.",
    steps=[{"color": "green", "value": None}, {"color": "orange", "value": 0.01},
           {"color": "red", "value": 0.05}]))
y += 4
panels.append(note(
    "Why there are no \"vs previous period\" deltas here", 0, y, 24, 4,
    "Grafana can put a delta on any of these tiles. None of them would be sound.\n\n"
    "- **The active-user tiles are gauges with a strong weekly cycle.** DAU swings from "
    "~1175 on a weekday to 60-160 at the weekend. \"Down 92% on yesterday\" on a Saturday "
    "is arithmetic, not information, and a same-weekday comparison needs a fixed 7d offset "
    "the tiles do not have. The day-of-week split in **Active users** below is the honest "
    "version of the same question.\n"
    "- **The counters reset on deploy.** A delta between two raw counter readings that "
    "straddle a pm2 reload is negative and meaningless. The rate panels below use "
    "increase(), which handles the reset; a tile-level delta does not.\n"
    "- **Signup and churn deltas need row-level history.** saas_users_total is current "
    "state, with no created_at. That question is answered properly in **Retention** below, "
    "against Postgres, where the cohort is a real signup week rather than a subtraction "
    "between two gauge readings."))
y += 4

# ── Active users ─────────────────────────────────────────────────
panels.append(row("Active users", y))
y += 1
panels.append(ts(
    "Daily active users, by day of week",
    # Two targets over the same gauge, each masked to one class of day. `and on()`
    # matches on the empty label set, so the single unlabelled day_of_week()
    # result gates the whole left-hand side. Each series is absent for the other's
    # steps, which is why spanNulls is off: the gap is the point.
    [(f'sum(saas_active_users{{window="1d", {GAUGE}}}) and on() {WEEKDAY}', "weekday"),
     (f'sum(saas_active_users{{window="1d", {GAUGE}}}) and on() {WEEKEND}', "weekend")],
    0, y, w=12, h=8, bars=True, span_nulls=False,
    desc="The same DAU gauge, drawn in two colours by day of week. Without this split the "
         "series is one line that collapses every Saturday, and the first person to open "
         "the dashboard on a Sunday opens an incident. Measured: ~1175 unique users on a "
         "weekday against 60-160 at the weekend, so the weekend floor is roughly a tenth "
         "of the weekday level and is normal. day_of_week() is evaluated in UTC while the "
         "dashboard renders in browser time, so for a UTC-3 viewer the colour changes at "
         "21:00 local rather than at midnight."))
panels.append(ts(
    "Active users by window",
    [(f'sum(saas_active_users{{window="1d", {GAUGE}}})', "1d (DAU)"),
     (f'sum(saas_active_users{{window="7d", {GAUGE}}})', "7d (WAU)"),
     (f'sum(saas_active_users{{window="30d", {GAUGE}}})', "30d (MAU)")],
    12, y, w=12, h=8,
    desc="All three windows on one axis. The wide windows are nearly flat while the 1d "
         "window oscillates weekly, which is the same day-of-week story stated a second "
         "way. Three gauges plotted directly: no rate(), and the 7d line is the backend's "
         "own distinct count, not seven 1d values added together."))
y += 8
panels.append(ts(
    "Stickiness (DAU/MAU)",
    [(f'100 * sum(saas_active_users{{window="1d", {GAUGE}}}) '
      f'/ sum(saas_active_users{{window="30d", {GAUGE}}})', "DAU/MAU")],
    0, y, w=8, h=7, unit="percent", dec=1,
    desc="Ratio of the 1d to the 30d distinct count. Compare a weekday against other "
         "weekdays: the numerator carries the weekly cycle and the denominator does not, "
         "so the ratio dips every weekend by construction."))
panels.append(ts(
    "Daily active users by company",
    [(f'sum by (company_slug) (saas_active_users{{window="1d", {GAUGE}}})',
      "{{company_slug}}")],
    8, y, w=8, h=7,
    desc="company_slug is the one tenant identifier that is safe as a Prometheus label: "
         "there are exactly two values, teem and reach, so it adds a factor of two to the "
         "series count rather than a factor of the user count. Present only on the "
         "aggregate gauges, which is why the counter panels are not broken down this way."))
panels.append(ts(
    "Active companies by window",
    [(f'sum(saas_active_companies{{window="1d", {ENV}}})', "1d"),
     (f'sum(saas_active_companies{{window="7d", {ENV}}})', "7d"),
     (f'sum(saas_active_companies{{window="30d", {ENV}}})', "30d")],
    16, y, w=8, h=7,
    desc="Tenants with at least one active user in each window. With two companies in "
         "total this is a small integer, so read it as a coverage check rather than a "
         "trend: 1 in the 1d window means one tenant was silent all day."))
y += 7
panels.append(sql_ts(
    "Daily active users from action_logs, by day of week",
    # count(DISTINCT) FILTER splits one scan into two series rather than running the
    # scan twice. nullif(...,0) is what keeps the inactive class absent instead of
    # drawn as a zero line: a weekday row genuinely has zero weekend users, and a
    # flat zero series next to the real one is just noise. With spanNulls off the two
    # series interleave into one readable bar chart.
    f"""SELECT date_trunc('day', a.created_at)                     AS time,
       nullif(count(DISTINCT a.user_id)
              FILTER (WHERE extract(isodow FROM a.created_at) <= 5), 0) AS weekday,
       nullif(count(DISTINCT a.user_id)
              FILTER (WHERE extract(isodow FROM a.created_at) >= 6), 0) AS weekend
  FROM action_logs a
  JOIN "Users" u ON u.id = a.user_id
 WHERE $__timeFilter(a.created_at)
   AND {NO_TEST}
 GROUP BY 1
 ORDER BY 1""",
    0, y, w=24, h=9, bars=True, span_nulls=False,
    desc="The measured DAU, from the activity rows themselves, with real history back to "
         "2024-03-05 rather than starting whenever the saas_* metrics deploy. This is the "
         "panel to check the Prometheus gauge against once both exist. " + ACTIVITY_DEF
         + " Days are UTC days, because created_at is timestamptz and the session runs at "
           "the server's UTC, while the dashboard renders in browser time: for a UTC-3 "
           "viewer a bar covers 21:00 the previous evening to 21:00. " + SCAN_NOTE
         + " " + PG_NOT_LIVE.replace("**", "")))
y += 9

# ── Logins and authentication ────────────────────────────────────
panels.append(row("Logins and authentication", y))
y += 1
panels.append(ts(
    "Login attempts per minute, by result",
    [(f'sum by (result) (rate(saas_login_attempts_total{{{ENV}, result=~"$result"}}[5m])) * 60',
      "{{result}}")],
    0, y, w=12, h=8, unit="short", dec=2,
    desc="rate() over a 5m window, scaled to a per-minute figure. Never the raw counter: "
         "pm2 reload zeroes it, so a raw reading is a per-deploy total masquerading as a "
         "cumulative one. result is success or failure."))
panels.append(ts(
    "Login success rate",
    [(f'(sum(rate(saas_login_attempts_total{{result="success", {ENV}}}[5m])) or vector(0)) '
      f'/ sum(rate(saas_login_attempts_total{{{ENV}}}[5m]))', "success rate")],
    12, y, w=12, h=8, unit="percentunit", dec=3, minv=0,
    desc="Successes over all attempts, both as rates so counter resets are handled. "
         "or vector(0) makes a clean window render as 0 rather than No data. At low "
         "traffic this is jumpy by construction: at three attempts in five minutes the "
         "series can only take the values 0, 1/3, 2/3 and 1."))
y += 8
panels.append(ts(
    "Login failures by reason",
    [(f'sum by (reason) (rate(saas_login_attempts_total{{result="failure", {ENV}}}[5m])) * 60',
      "{{reason}}")],
    0, y, w=12, h=8, stack=True, dec=2,
    desc="Stacked, so the total failure rate and its composition read off the same panel. "
         "The reason set is closed and comes from the login path in backend/services/"
         "auth.services.js: user_not_found (no such email; there is no auto-provisioning), "
         "bad_password (a returning user's bcrypt compare failed), firebase_rejected (an "
         "imported user's first login, rejected by Firebase), no_firebase_config (an "
         "imported user's first login at a company with Firebase auth disabled, so there "
         "is nothing to fall back to), deactivated (past the deactivation grace period). "
         "user_not_found rising on its own is credential stuffing; bad_password rising on "
         "its own is more often a broken client or a password-reset regression."))
panels.append(ranked(
    "Failure reasons over the range",
    # label_replace still names the empty bucket rather than drawing an anonymous
    # bar: a failure recorded without a reason is a gap in the instrumentation and
    # is worth seeing.
    f'label_replace(topk(10, sum by (reason) '
    f'(increase(saas_login_attempts_total{{result="failure", {ENV}}}[$__range])))'
    f', "reason", "(no reason label)", "reason", "^$")',
    12, y, w=12, h=8,
    desc="Failure counts by reason over the dashboard's time range, from increase() so "
         "deploys inside the range do not truncate the total. Instant rather than range: "
         "a top-N re-evaluated at every step renders as hundreds of slivers."))
y += 8
panels.append(logs(
    "Authentication events",
    # The `"module":"auth"` alternative is not redundant with the operation regex, it
    # is what makes the panel complete. Checked against 24h of production: the
    # operation-only version matched login / resetPassword / verifyMagicLink but
    # missed the magic-link lines whose operation is bare `generate` or `verify`,
    # which are auth events with no auth word in the operation name. The `event`
    # alternative is for the field the saas product metrics work adds, so the panel
    # has content either side of that deploy.
    '{job=~"$loki_job"} '
    r'|~ `"module":"auth"|"(event|operation)":"[^"]*(login|auth|password|magic)`'
    + " " + PARSED + LOG_FIELDS + ' | provider=~"$auth_method"',
    0, y, h=10,
    desc="Auth log lines: selector, then a regex line filter over the raw bytes, then "
         "| json. The parser is the expensive stage and runs last, on the lines that "
         "survived. Verified against live data: this returns real lines today "
         "(module=auth with operation login, resetPassword, verifyMagicLink, generate and "
         "verify). Only info, warn and error are ever here, because production runs at "
         "level info; the Firebase rejection path logs at debug, so a firebase_rejected "
         "failure appears in the metric panels above with no matching line here. The Auth "
         "method filter binds to this panel and nothing else, and only its firebase value "
         "narrows anything: see the variable's own description."))
y += 10

# ── Feature usage ────────────────────────────────────────────────
panels.append(row("Feature usage", y))
y += 1
panels.append(ts(
    "Feature events per minute, by feature",
    [(f'sum by (feature) (rate(saas_feature_usage_total'
      f'{{{ENV}, feature=~"$feature", user_role=~"$user_role"}}[5m])) * 60', "{{feature}}")],
    0, y, w=12, h=8, dec=2,
    desc="Events, not users. One user hitting a feature forty times in a session is forty "
         "here. Read it as load and as a relative ranking between features, never as "
         "reach."))
panels.append(ranked(
    "Top features over the range",
    f'topk(15, sum by (feature) (increase(saas_feature_usage_total'
    f'{{{ENV}, feature=~"$feature", user_role=~"$user_role"}}[$__range])))',
    12, y, w=12, h=8,
    desc="Event totals per feature across the range, from increase(). Same caveat: these "
         "are event counts. A feature used constantly by three people outranks one used "
         "once by three hundred."))
y += 8
panels.append(ts(
    "Feature events per minute, by role",
    [(f'sum by (user_role) (rate(saas_feature_usage_total'
      f'{{{ENV}, feature=~"$feature", user_role=~"$user_role"}}[5m])) * 60', "{{user_role}}")],
    0, y, w=12, h=8, dec=2,
    desc="The four roles are super_admin, team_member, manager and representative "
         "(the enum in backend/models/user.model.js). Note the label is user_role on this "
         "counter and role on saas_users_total; the Role filter binds to both."))
panels.append(note(
    "Adoption rate is deliberately not a panel here", 12, y, 12, 8,
    "**Adoption** is *unique users who used a feature* over *eligible active users* "
    "- eligible, not all, because several features are role-scoped and dividing by the "
    "whole user base understates them by whatever share of users could never reach the "
    "feature.\n\n"
    "Neither term is available:\n\n"
    "- **The numerator needs unique users per feature.** saas_feature_usage_total is a "
    "counter of events. Deriving distinct users from it is impossible in principle, and "
    "adding a user id label to make it possible would multiply the series count by the "
    "user count.\n"
    "- **The denominator needs role eligibility per feature.** That mapping lives in the "
    "application's authorisation rules, not in any metric.\n\n"
    "The panels to the left are therefore labelled *events*, and no ratio is derived from "
    "them. A unique-user numerator is computable in Postgres from the feature usage rows; "
    "see the blocked rows below."))
y += 8

# ── Companies and tenants ────────────────────────────────────────
panels.append(row("Companies and tenants", y))
y += 1
panels.append(stat(
    "Companies", f'max(saas_companies_total{{{ENV}}})', 0, y, w=6,
    desc="Tenant companies in the database. max() deduplicates the api and cron copies of "
         "the same gauge."))
panels.append(stat(
    "Active companies (30d)", f'sum(saas_active_companies{{window="30d", {ENV}}})', 6, y, w=6,
    desc="Companies with at least one user active in the 30d window."))
panels.append(stat(
    "Company activation (30d)",
    f'100 * sum(saas_active_companies{{window="30d", {ENV}}}) '
    f'/ max(saas_companies_total{{{ENV}}})',
    12, y, w=6, unit="percent", dec=0,
    desc="Share of tenants with any activity in 30 days. With two companies this is 0, 50 "
         "or 100 and nothing else, so treat it as a flag rather than a trend."))
panels.append(stat(
    "Users per company (mean)",
    f'sum(saas_users_total{{{ENV}}}) / max(saas_companies_total{{{ENV}}})',
    18, y, w=6, dec=1,
    desc="Arithmetic mean, and with two tenants of very different size the mean is not "
         "near either of them. The per-company breakdown below is the number to actually "
         "read."))
y += 4
panels.append(ts(
    "Registered users by company",
    [(f'sum by (company_slug) (saas_users_total'
      f'{{{GAUGE}, role=~"$user_role", status=~"$user_status"}})', "{{company_slug}}")],
    0, y, w=12, h=8,
    desc="Account counts per tenant, filtered by role and status. Safe on Prometheus "
         "because company_slug has two values; this is the only tenant dimension that goes "
         "into a metric query anywhere on this dashboard."))
panels.append(ts(
    "Monthly engagement by company",
    # Both sides carry company_slug, so the division matches per tenant with no on()
    # or ignoring() clause. The GAUGE selector is on both operands so the environment
    # and company filters narrow the numerator and denominator together; filtering
    # only one would produce a ratio above 1.
    [(f'100 * sum by (company_slug) (saas_active_users{{window="30d", {GAUGE}}}) '
      f'/ sum by (company_slug) (saas_users_total{{{GAUGE}}})', "{{company_slug}}")],
    12, y, w=12, h=8, unit="percent", dec=1,
    desc="Share of each tenant's accounts that were active in the 30d window. This is the "
         "one per-tenant ratio the aggregate gauges can support honestly, because both "
         "operands carry company_slug and both are distinct counts. Note the denominator "
         "here is every account including never-activated ones, so a tenant mid-onboarding "
         "reads low for reasons that are not churn - splitting those apart needs the "
         "blocked table below."))
y += 8
panels.append(table(
    "Per-company detail",
    # The activity join is a bounded subquery rather than a join straight onto
    # action_logs: DISTINCT user_id inside the time range collapses 2.1M rows to at
    # most a few thousand before the join, so the outer count(DISTINCT) does not have
    # to deduplicate the whole scan. Joining action_logs directly would also multiply
    # the user rows and break every other count in the SELECT.
    f"""WITH active AS (
  SELECT DISTINCT a.user_id
    FROM action_logs a
   WHERE $__timeFilter(a.created_at)
)
SELECT c.name                                                        AS company,
       c.slug,
       count(DISTINCT t.id)                                          AS tenants,
       count(DISTINCT u.id)                                          AS users,
       count(DISTINCT u.id) FILTER (WHERE act.user_id IS NOT NULL)    AS "active in range",
       count(DISTINCT u.id) FILTER (WHERE u.role = 'team_member')     AS team_members,
       count(DISTINCT u.id) FILTER (WHERE u.role = 'manager')         AS managers,
       count(DISTINCT u.id) FILTER (WHERE u.role = 'representative')  AS representatives,
       count(DISTINCT u.id) FILTER (WHERE u.role = 'super_admin')     AS super_admins,
       count(DISTINCT u.id) FILTER (WHERE u.last_login IS NULL)       AS "never logged in",
       count(DISTINCT u.id) FILTER (WHERE u.deactivated_at IS NOT NULL) AS deactivated
  FROM companies c
  JOIN "Tenants" t   ON t."CompanyId" = c.id
  JOIN "Users"   u   ON u."TenantId"  = t.id
  LEFT JOIN active act ON act.user_id = u.id
 WHERE {NO_TEST}
 GROUP BY c.name, c.slug
 ORDER BY users DESC""",
    0, y, w=24, h=9,
    desc="One row per tenant company. Only 'active in range' depends on the dashboard's "
         "time range; every other column is current state, which is why the row counts do "
         "not move when the range does. Users with no TenantId are absent by construction, "
         "because the join runs through \"Tenants\" and they belong to no company: in "
         "practice that is the super_admins, so these counts sum to slightly less than the "
         "whole user base and are not meant to match Registered users above. "
         + ACTIVITY_DEF + " Seats sold is not a column "
         "here and cannot be: there is no plan or seat field anywhere in "
         "performance-tracker backend/models/, so 'seats used against seats bought' has no "
         "second term. See the Plan variable's description. " + PG_NOT_LIVE.replace("**", "")))
y += 9

# ── Most active users ────────────────────────────────────────────
# The list itself is on the restricted dashboard, because a leaderboard is a list of
# names. What stays here is the shape of the distribution, which answers "is usage
# concentrated in a handful of people" without naming any of them.
panels.append(row("Most active users (aggregate)", y))
y += 1
panels.append(table(
    "Activity concentration",
    # width_bucket would give evenly spaced buckets, which is wrong for a
    # distribution this skewed: almost every user would land in the first one. The
    # CASE ladder is deliberately log-ish, so the tail is legible.
    f"""WITH per_user AS (
  SELECT a.user_id, count(*) AS actions
    FROM action_logs a
    JOIN "Users" u ON u.id = a.user_id
   WHERE $__timeFilter(a.created_at)
     AND {NO_TEST}
   GROUP BY a.user_id
)
SELECT CASE WHEN actions = 1           THEN '1'
            WHEN actions <= 5          THEN '2-5'
            WHEN actions <= 20         THEN '6-20'
            WHEN actions <= 100        THEN '21-100'
            WHEN actions <= 500        THEN '101-500'
            ELSE                            '500+'
       END                                              AS "actions in range",
       count(*)                                         AS users,
       round(100.0 * count(*) / sum(count(*)) OVER (), 1) AS "% of active users",
       sum(actions)                                     AS actions,
       round(100.0 * sum(actions) / sum(sum(actions)) OVER (), 1) AS "% of all actions"
  FROM per_user
 GROUP BY 1
 ORDER BY min(actions)""",
    0, y, w=12, h=9,
    desc="How concentrated activity is, without naming anyone. Read the last two columns "
         "together: if the 500+ bucket is 2% of users and 60% of actions, the product has a "
         "small number of power users and a long tail, and an average-actions-per-user "
         "figure would describe neither group. " + ACTIVITY_DEF + " " + SCAN_NOTE
         + " " + PG_NOT_LIVE.replace("**", "")))
panels.append(note(
    "The per-user leaderboard is on the restricted dashboard", 12, y, 12, 9,
    "The named leaderboard - name, email, company, role, actions, logins, distinct active "
    "days, last seen - is on **SaaS Product Analytics: Users** "
    "(`/d/pt-saas-analytics-users`), reachable from the dashboard links at the top of this "
    "page.\n\n"
    "**Why it is not here.** That table carries email addresses and full names. Grafana "
    "grants access per dashboard, not per panel, so a single dashboard mixing aggregate and "
    "identifiable panels can only be permissioned as though all of it were identifiable. "
    "Splitting them means this dashboard can be shared with anyone who needs product "
    "numbers, and the user list can be restricted to the people who need to act on "
    "individuals.\n\n"
    "**It is not blocked and not approximated.** The query exists and runs against "
    "`action_logs` joined to `\"Users\"`; it is simply behind a different permission."))
y += 9

# ── Inactive users ───────────────────────────────────────────────
panels.append(row("Inactive users (aggregate)", y))
y += 1
panels.append(table(
    "Inactive user counts",
    # Reads last_login on "Users" rather than scanning action_logs, so it is a 5384
    # row query with no time bound and no scan. It also means the buckets follow
    # last_login, which the login path maintains, and not the mutating-action rows.
    f"""SELECT CASE WHEN u.last_login IS NULL                            THEN 'never logged in'
            WHEN u.last_login <  now() - interval '90 days'        THEN 'quiet 90+ days'
            WHEN u.last_login <  now() - interval '60 days'        THEN 'quiet 60-90 days'
            WHEN u.last_login <  now() - interval '30 days'        THEN 'quiet 30-60 days'
            ELSE                                                       'active within 30 days'
       END                                                  AS status,
       count(*)                                             AS users,
       count(*) FILTER (WHERE u.deactivated_at IS NOT NULL)  AS "of which deactivated",
       round(100.0 * count(*) / sum(count(*)) OVER (), 1)    AS "% of all users"
  FROM "Users" u
  WHERE {NO_TEST}
 GROUP BY 1
 ORDER BY 2 DESC""",
    0, y, w=12, h=9,
    desc="The size of each inactive population, from Users.last_login. Never-logged-in and "
         "gone-quiet are separated because they are different problems wearing the same "
         "symptom: the first is an onboarding failure, the second is a churn signal. The "
         "deactivated column matters for the same reason - a deactivated user is not a "
         "churn signal, they were switched off deliberately. Measured at the time of "
         "writing: 2156 of 5384 users have never logged in and 1381 are deactivated. This "
         "query reads last_login rather than action_logs, so it needs no time bound and no "
         "scan. " + PG_NOT_LIVE.replace("**", "")))
panels.append(note(
    "The named inactive-user list is on the restricted dashboard", 12, y, 12, 9,
    "The list itself - name, email, company, role, signup date, last login, days quiet - is "
    "on **SaaS Product Analytics: Users** (`/d/pt-saas-analytics-users`), linked at the top "
    "of this page.\n\n"
    "**Why it is split.** The counts on the left answer \"how big is the problem\". The list "
    "answers \"who do I email\", and that is a column of email addresses. Grafana "
    "permissions a dashboard, not a panel, so the two live apart and only the second one "
    "needs restricting.\n\n"
    "**A caveat that applies to both.** `last_login` is maintained by the login path, so a "
    "user who is quiet by this measure has genuinely not logged in. But the reverse is "
    "weaker: because successful reads are recorded nowhere, someone who logs in and only "
    "reads leaves a fresh `last_login` and no `action_logs` rows, so they count as active "
    "here and as inactive in the action-based panels. The two definitions disagree by "
    "design, and this is where that shows."))
y += 9

# ── Retention ────────────────────────────────────────────────────
panels.append(row("Retention", y))
y += 1
panels.append(table(
    "Retention by signup cohort",
    # One aggregate per user (max(created_at)) instead of a row-per-activity join, so
    # the FILTER clauses run over 5384 rows rather than over the activity scan. The
    # LEFT JOIN is what keeps a cohort member who never came back in the denominator;
    # an inner join would silently drop them and inflate every percentage.
    f"""WITH cohort AS (
  SELECT u.id                                      AS user_id,
         date_trunc('month', u."createdAt")        AS cohort_month,
         u."createdAt"                             AS signed_up
    FROM "Users" u
   WHERE $__timeFilter(u."createdAt")
     AND {NO_TEST}
),
last_seen AS (
  SELECT c.user_id,
         c.cohort_month,
         c.signed_up,
         max(a.created_at) AS last_action
    FROM cohort c
    LEFT JOIN action_logs a
           ON a.user_id = c.user_id
          AND a.created_at >= c.signed_up
          AND a.created_at <  now()
   GROUP BY 1, 2, 3
)
SELECT to_char(cohort_month, 'YYYY-MM')                                AS cohort,
       count(*)                                                        AS "signed up",
       count(*) FILTER (WHERE last_action IS NOT NULL)                  AS "ever active",
       round(100.0 * count(*) FILTER (
             WHERE last_action >= signed_up + interval  '7 days') / count(*), 1) AS "d7 %",
       round(100.0 * count(*) FILTER (
             WHERE last_action >= signed_up + interval '30 days') / count(*), 1) AS "d30 %",
       round(100.0 * count(*) FILTER (
             WHERE last_action >= signed_up + interval '90 days') / count(*), 1) AS "d90 %",
       -- The age of the cohort's YOUNGEST member, from max(signed_up), not the age of
       -- the month bucket. Measuring from the month start would report the oldest
       -- possible age and so overstate how mature the cohort is: a bucket 95 days old
       -- can hold a member who joined 65 days ago and cannot have reached d90. This
       -- way the column is the conservative bound, and a value >= 90 means the d90
       -- column is trustworthy for every row in the cohort.
       (now()::date - max(signed_up)::date)                             AS "youngest member age days"
  FROM last_seen
 GROUP BY cohort_month
 ORDER BY cohort_month DESC""",
    0, y, w=24, h=10,
    desc="Rolling retention by signup month: of the users who signed up in a month, the "
         "share with any activity at least N days after their own signup timestamp. Each "
         "user is measured from their own signup, not from the start of the month, so "
         "someone who joined on the 28th is not penalised. Because the condition is \"at "
         "least N days later\", the columns fall monotonically and a user active at d90 is "
         "counted at d7 and d30 too. **Read the age column before the percentages:** a "
         "cohort whose youngest member is under 90 days old cannot yet have reached d90, so "
         "its d90 reads low for arithmetic reasons and not because those users churned. The "
         "column reports the youngest member rather than the age of the month bucket, so a "
         "value at or above the window is a guarantee for every row in the cohort and not "
         "just for its oldest. The bottom rows are the trustworthy ones. " + ACTIVITY_DEF + " Note the consequence: a user who signs "
         "up, logs in, reads, and never mutates anything counts as retained only through "
         "the login rows. " + SCAN_NOTE + " " + PG_NOT_LIVE.replace("**", "")))
y += 10
panels.append(sql_ts(
    "Weekly active users, split into new / retained / resurrected",
    # The self-join on act is the previous-week lookup. It is cheap because `act` is
    # already a DISTINCT (user, week) set, not the raw rows. Classification order
    # matters: new is tested first, so a user active in their signup week is counted
    # once as new rather than also as resurrected.
    f"""WITH act AS (
  SELECT DISTINCT a.user_id,
         date_trunc('week', a.created_at) AS wk
    FROM action_logs a
    JOIN "Users" u ON u.id = a.user_id
   WHERE $__timeFilter(a.created_at)
     AND {NO_TEST}
),
signup AS (
  SELECT u.id AS user_id, date_trunc('week', u."createdAt") AS signup_wk
    FROM "Users" u
   WHERE {NO_TEST}
)
SELECT a.wk                                                                AS time,
       count(*) FILTER (WHERE s.signup_wk = a.wk)                           AS new,
       count(*) FILTER (WHERE s.signup_wk <> a.wk
                          AND p.user_id IS NOT NULL)                        AS retained,
       count(*) FILTER (WHERE s.signup_wk <> a.wk
                          AND p.user_id IS NULL)                            AS resurrected
  FROM act a
  JOIN signup s ON s.user_id = a.user_id
  LEFT JOIN act p ON p.user_id = a.user_id
                 AND p.wk     = a.wk - interval '1 week'
 GROUP BY a.wk
 ORDER BY a.wk""",
    0, y, w=24, h=9, stack=True, bars=True,
    desc="Every active user each week, classified: new signed up that week, retained were "
         "also active the week before, resurrected came back after at least one week away. "
         "Stacked, so the total is WAU and the composition is visible in the same panel - "
         "a flat total made of a rising 'resurrected' and a falling 'retained' is a "
         "retention problem hidden behind a healthy headline. 'new' is a real signup week "
         "from Users.createdAt, not merely first-seen inside the selected range, so "
         "narrowing the range does not manufacture new users. The first week shown always "
         "understates 'retained', because its previous week is outside the range and the "
         "lookup finds nothing. " + ACTIVITY_DEF + " " + SCAN_NOTE + " "
         + PG_NOT_LIVE.replace("**", "")))
y += 9

# ── Sessions and engagement ──────────────────────────────────────
# The engagement half of this section is built: it is the two Retention panels above
# and Activity concentration. What remains genuinely impossible is session duration,
# and the row exists to say why rather than to leave a gap someone fills in with a
# JWT-expiry chart.
panels.append(row("Sessions and engagement (session duration blocked)", y))
y += 1
panels.append(sql_ts(
    "Actions per active user per day",
    # The engagement metric that needs no session concept at all. Two series from one
    # scan: the mean is what gets asked for, the p90 is what makes the mean readable,
    # because this distribution is skewed enough that they diverge.
    f"""WITH per_user_day AS (
  SELECT a.user_id,
         date_trunc('day', a.created_at) AS d,
         count(*)                        AS actions
    FROM action_logs a
    JOIN "Users" u ON u.id = a.user_id
   WHERE $__timeFilter(a.created_at)
     AND {NO_TEST}
   GROUP BY 1, 2
)
SELECT d                                                          AS time,
       round(avg(actions), 2)                                     AS "mean actions/user",
       percentile_cont(0.5) WITHIN GROUP (ORDER BY actions)        AS "median actions/user",
       percentile_cont(0.9) WITHIN GROUP (ORDER BY actions)        AS "p90 actions/user"
  FROM per_user_day
 GROUP BY d
 ORDER BY d""",
    0, y, w=12, h=10, dec=2,
    desc="Engagement depth per active day, over users who were active that day. The mean, "
         "median and p90 are all here because they disagree: with a skewed distribution the "
         "mean sits above the median and describes nobody, and quoting it alone is the "
         "usual way this number misleads. Needs no session concept, so unlike session "
         "duration it is computable today. " + ACTIVITY_DEF + " " + SCAN_NOTE + " "
         + PG_NOT_LIVE.replace("**", "")))
panels.append(note(
    "Session duration cannot be computed, and this is not a datasource problem", 12, y, 12, 10,
    "**What is missing:** sessions per user, median and p90 session duration, actions per "
    "session, time-to-first-action after login.\n\n"
    "**Why the Postgres datasource does not unblock it.** A duration needs a start and an "
    "end. Only the start is recorded:\n\n"
    "- **There is no session table.** Nothing persists a session row to close, so there is "
    "nothing to read.\n"
    "- **There is no logout event.** Clients drop the token and the backend is never told, "
    "so no end timestamp is ever written anywhere.\n"
    "- **JWT expiry must not be substituted.** It is a fixed constant from the signing "
    "config, so every session would come out exactly that long. The chart would be a "
    "restatement of a configuration value, and it would be read and quoted as a "
    "measurement of user behaviour.\n\n"
    "**What it needs, in order.** First an application change, and it is a product decision "
    "before it is an engineering one: either emit a session lifecycle event, or agree a "
    "definition by inactivity timeout (for instance, 30 minutes with no request closes the "
    "session) and sessionise `action_logs` against it. Someone has to decide what a session "
    "*is* before any number is trustworthy. Only then is it a query.\n\n"
    "**Available without that decision:** the panel to the left, and time-to-first-action, "
    "which is a login row and the next action row for the same user - a plain window "
    "function over `action_logs`, needing no new instrumentation. It is not here yet because "
    "no query on this dashboard has been executed against the database at all."))
y += 10

# ── Product health ───────────────────────────────────────────────
panels.append(row("Product health", y))
y += 1
panels.append(ts(
    "Requests per second, by route",
    # topk before sum by: 137 route values would otherwise render as 137 series.
    # The inner rate is already per-series, so topk picks the busiest instances and
    # sum by (route) folds any duplicates that survive.
    [('sum by (route) (topk(15, rate(http_request_duration_seconds_count{job="api"}[5m])))',
      "{{route}}")],
    0, y, w=12, h=8, unit="reqps", dec=2,
    desc="Top 15 mounted routes by request rate. Concrete URLs never become labels - the "
         "route label is the Express mount path, so /api/tasks/:id is one series and not "
         "one per task. There are 137 route values in Prometheus today, hence the topk."))
panels.append(ts(
    "Latency p50 / p95 / p99",
    [('histogram_quantile(0.50, sum by (le) (rate(http_request_duration_seconds_bucket'
      '{job="api"}[5m])))', "p50"),
     ('histogram_quantile(0.95, sum by (le) (rate(http_request_duration_seconds_bucket'
      '{job="api"}[5m])))', "p95"),
     ('histogram_quantile(0.99, sum by (le) (rate(http_request_duration_seconds_bucket'
      '{job="api"}[5m])))', "p99")],
    12, y, w=12, h=8, unit="s", dec=3,
    desc="Percentiles interpolated over the histogram buckets, all routes combined. The "
         "top bucket is 10s, so anything slower is reported as 10s rather than as its real "
         "value."))
y += 8
panels.append(ts(
    "5xx rate",
    [('(sum(rate(http_request_duration_seconds_count{job="api", status_code=~"5.."}[5m])) '
      'or vector(0)) / sum(rate(http_request_duration_seconds_count{job="api"}[5m]))', "5xx")],
    0, y, w=8, h=7, unit="percentunit", dec=4,
    desc="Share of requests answered with 5xx. or vector(0) so a healthy window renders as "
         "zero rather than as No data, which would look identical to a dead scrape."))
panels.append(ts(
    "Requests per second, by status",
    [('sum by (status_code) (rate(http_request_duration_seconds_count{job="api"}[5m]))',
      "{{status_code}}")],
    8, y, w=8, h=7, unit="reqps", dec=2,
    desc="4xx climbing before 5xx does is the usual shape of a client or auth regression, "
         "which is why this is next to the 5xx panel rather than folded into it."))
panels.append(ts(
    "Auth endpoint latency p95",
    [('histogram_quantile(0.95, sum by (le, route) (rate('
      'http_request_duration_seconds_bucket{job="api", route=~"/api/auth/.*"}[5m])))',
      "{{route}}")],
    16, y, w=8, h=7, unit="s", dec=3,
    desc="p95 per auth route. Slow here is felt as \"the product is broken\" even when "
         "everything else is fine, and the login path does synchronous work on the way "
         "through (a HubSpot sync on login), so it is worth its own panel."))
y += 7

# ── Logs ─────────────────────────────────────────────────────────
panels.append(row("Logs", y))
y += 1
panels.append(ts(
    "Product event volume by event",
    ['sum by (event) (count_over_time({job=~"$loki_job"} |= `"event":"` '
     + PARSED + LOG_FIELDS + " [$__interval]))"],
    0, y, w=12, h=8, stack=True, bars=True, datasource=LOKI,
    desc="Structured product events per interval. The `\"event\":\"` line filter runs "
         "before the parser and cuts the volume by roughly the ratio of product events to "
         "all logging, so | json only ever sees the candidate lines. Empty until the "
         "backend emits an `event` field - verified against the live stack, where this "
         "query parses and returns zero series today."))
panels.append(ts(
    "Log volume by level",
    ['sum by (level) (count_over_time({job=~"$loki_job"} ' + PINO + " " + PARSED
     + ' | level=~"$level" [$__interval]))'],
    12, y, w=12, h=8, stack=True, bars=True, datasource=LOKI,
    desc="Volume by level, read from the level field rather than from which stream the "
         "line arrived on: pino writes every level to stdout, so stderr carries no "
         "application errors. There is no debug bar and there never will be in production "
         "- backend/utils/logger.js pins the level to info there. Measured over one hour on "
         "the live stack: 3327 info, 581 warn, 28 error, 0 debug. Successful request traces "
         "are logged at debug and so are absent from Loki entirely."))
y += 8
panels.append(logs(
    "Product events",
    '{job=~"$loki_job"} ' + PINO + " " + PARSED + LOG_FIELDS,
    0, y, h=14,
    desc="The structured stream behind the panels above, filtered by the variable bar. "
         "Company and user filtering happens here and only here: companyId and userId are "
         "body fields reachable after | json, never stream labels, and they are the two "
         "identifiers that must not become Prometheus labels. Both the api process (Loki "
         "job `server`, since Alloy names streams after the PM2 process) and the cron "
         "process (job `cron`) are in scope - Prometheus calls the same processes `api` and "
         "`cron`, so the Process filter here is Loki's naming, not Prometheus's. Expand a "
         "line for requestId and correlationId to follow one request across services."))

dashboard = {
    "uid": "pt-saas-analytics",
    "title": "SaaS Product Analytics",
    "description": "AGGREGATE product view of the Performance Tracker: active users, "
                   "logins, feature usage, tenants and retention. Nothing on this dashboard "
                   "identifies an individual, so it can be shared with anyone who needs "
                   "product numbers. The named user lists live on 'SaaS Product Analytics: "
                   "Users' (uid pt-saas-analytics-users), which is separate precisely so it "
                   "can be permissioned separately: Grafana grants access per dashboard, "
                   "not per panel. Sources: Prometheus for the saas_* metrics and the api "
                   "job's HTTP histogram, PostgreSQL for retention and the per-company "
                   "table, Loki for the event log. The saas_* metrics land with the backend "
                   "branch feat/saas-product-metrics and the Postgres tunnel is not up yet, "
                   "so both sets of panels are empty until then. Log lines carry PII and "
                   "Loki retention is 30 days.",
    "tags": ["performance-tracker", "observability", "product", "analytics"],
    # keepTime and includeVars so following the link does not silently reset the range
    # or the filters: the two dashboards share every variable name for that reason.
    "links": [
        {"type": "link", "title": "Named user lists (restricted)",
         "url": "/d/pt-saas-analytics-users/", "tooltip":
             "Most active users and inactive users, with names and email addresses. "
             "Separate dashboard so it can be permissioned separately.",
         "icon": "external link", "targetBlank": False, "asDropdown": False,
         "includeVars": True, "keepTime": True, "tags": []},
        {"type": "link", "title": "API and cron metrics",
         "url": "/d/pt-api/", "tooltip": "Infrastructure view of the same two processes.",
         "icon": "external link", "targetBlank": False, "asDropdown": False,
         "includeVars": False, "keepTime": True, "tags": []},
    ],
    "timezone": "browser",
    "editable": True,
    "schemaVersion": 39,
    "version": 1,
    "refresh": "5m",
    # 7d, not 6h: at 6h a dashboard whose headline metric has a weekly cycle opens
    # showing a fragment of one day, which is the framing that makes a weekend look
    # like an outage. A week is the shortest range in which the cycle is visible.
    "time": {"from": "now-7d", "to": "now"},
    "panels": panels,
    "templating": {"list": [
        {
            # All by default, and allValue ".*" rather than a literal list: before the
            # backend deploys, label_values returns nothing, and an empty single-select
            # would interpolate to environment=~"" and match only series with no
            # environment label. environment=~".*" matches everything including the
            # absent case, so the dashboard degrades to "unfiltered" instead of "empty".
            "name": "environment", "label": "Environment", "type": "query", "datasource": PROM,
            "query": {"query": "label_values(saas_users_total, environment)", "refId": "A"},
            "description": "Prometheus label on every saas_* metric, and a pino field on "
                           "every log line, so this one filter reaches both datasources.",
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]}, "refresh": 1,
        },
        {
            "name": "company_slug", "label": "Company (metrics)", "type": "query",
            "datasource": PROM,
            "query": {"query": "label_values(saas_users_total, company_slug)", "refId": "A"},
            "description": "The only tenant identifier safe to put in a Prometheus query: "
                           "two values, teem and reach. It is present on the aggregate "
                           "gauges only, so selecting one company narrows the user and "
                           "active-user panels and leaves the login and feature counters "
                           "unnarrowed - those have no company_slug label.",
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]}, "refresh": 1,
        },
        {
            # Textbox rather than query: companyId is a JSON body field, and Loki's
            # label_values only enumerates stream labels, so there is nothing to
            # populate a dropdown from. Default .* rather than empty, because
            # companyId=~"" matches only lines that have no companyId at all.
            "name": "company_id", "label": "Company ID (logs)", "type": "textbox",
            "query": ".*", "current": {"text": ".*", "value": ".*"},
            "description": "Tenant UUID, applied to the Loki panels only. Never reaches a "
                           "Prometheus query: a company id as a metric label would be a "
                           "cardinality decision, and the aggregate metrics are not scoped "
                           "this way on purpose.",
        },
        {
            "name": "user_id", "label": "User ID (logs)", "type": "textbox",
            "query": ".*", "current": {"text": ".*", "value": ".*"},
            "description": "User UUID, applied to the Loki panels only. A user id must "
                           "never appear in a Prometheus query - at ~1175 daily active "
                           "users it turns one counter into thousands of series.",
        },
        {
            "name": "plan", "label": "Plan (inert)", "type": "custom",
            "query": "no plan field exists",
            "options": [{"text": "no plan field exists", "value": "no plan field exists",
                         "selected": True}],
            "current": {"text": "no plan field exists", "value": "no plan field exists"},
            "description": "Wired to nothing, and honestly labelled. The datasource is no "
                           "longer the obstacle - the obstacle is that there is no plan "
                           "column to query. backend/models/company.model.js has id, name, "
                           "slug, mandatory_journey_days and a config JSONB, and no model in "
                           "backend/models/ carries a plan or tier field. The Grafana role "
                           "can read companies, so the moment the column exists this becomes "
                           "a one-line query variable. Until then it narrows nothing, and it "
                           "is wired to no query rather than wired to a guess.",
        },
        {
            "name": "user_role", "label": "User role", "type": "query", "datasource": PROM,
            "query": {"query": "label_values(saas_users_total, role)", "refId": "A"},
            "description": "One variable, two label names: `role` on saas_users_total and "
                           "`user_role` on saas_feature_usage_total. Values come from the "
                           "enum in backend/models/user.model.js: super_admin, team_member, "
                           "manager, representative.",
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]}, "refresh": 1,
        },
        {
            "name": "user_status", "label": "User status", "type": "query", "datasource": PROM,
            "query": {"query": "label_values(saas_users_total, status)", "refId": "A"},
            "description": "Account status from saas_users_total. Reaches the registered-user "
                           "panels only: the activity gauges are not broken down by status.",
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]}, "refresh": 1,
        },
        {
            "name": "feature", "label": "Feature", "type": "query", "datasource": PROM,
            "query": {"query": "label_values(saas_feature_usage_total, feature)", "refId": "A"},
            "description": "Feature label on saas_feature_usage_total, and the `feature` "
                           "field on the log lines.",
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]}, "refresh": 1,
        },
        {
            # Custom rather than query: the set is closed in the backend, so it should
            # be usable before the metric has ever been scraped.
            "name": "result", "label": "Result", "type": "custom",
            "query": "success,failure",
            "options": [{"text": "success", "value": "success", "selected": False},
                        {"text": "failure", "value": "failure", "selected": False}],
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]},
            "description": "result label on saas_login_attempts_total, and the `result` "
                           "field on the log lines.",
        },
        {
            # One option, not three, and that is the honest list. `provider` is written
            # at exactly three call sites, all in backend/services/auth.services.js and
            # all with the value 'firebase'; nothing anywhere sets it to 'local' or
            # 'magic_link'. Offering those two would give a filter that always returns
            # nothing, which reads as "no traffic" rather than as "no such field" and is
            # precisely the failure mode this repo has already shipped three times.
            "name": "auth_method", "label": "Auth method (logs)", "type": "custom",
            "query": "firebase",
            "options": [{"text": "firebase", "value": "firebase", "selected": False}],
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]},
            "description": "Loki-only, on the logger's `provider` field, and firebase is "
                           "the only value that exists. Local bcrypt and magic-link logins "
                           "set no auth-method field at all, so they cannot be filtered "
                           "here - they are visible in the panel, distinguishable by "
                           "`operation`, but not selectable. Even firebase is sparse: "
                           "measured on the live stack, zero of 2.19 million lines over 7 "
                           "days carry a `provider` field, because it is only written on an "
                           "imported user's first login and the rejection case is logged at "
                           "debug, which production never emits. Filtering the metric panels "
                           "by auth method is not possible either: saas_login_attempts_total "
                           "has no auth_method label. The nearest signal there is the reason "
                           "breakdown, where firebase_rejected and no_firebase_config are "
                           "Firebase-specific and bad_password is local.",
        },
        {
            "name": "event", "label": "Event type (logs)", "type": "textbox",
            "query": ".*", "current": {"text": ".*", "value": ".*"},
            "description": "Regex against the `event` field. A textbox and not a dropdown "
                           "because `event` is a JSON body field and Loki's label_values "
                           "only enumerates stream labels, so there is nothing to populate "
                           "a list from without querying every line.",
        },
        {
            "name": "level", "label": "Log level", "type": "custom",
            "query": "error,warn,info",
            "options": [{"text": "error", "value": "error", "selected": False},
                        {"text": "warn", "value": "warn", "selected": False},
                        {"text": "info", "value": "info", "selected": False}],
            "multi": True, "includeAll": True, "allValue": ".*",
            "current": {"text": ["All"], "value": ["$__all"]},
            "description": "No debug option, because production never emits it: "
                           "backend/utils/logger.js pins the level to info outside "
                           "development. Product events are logged at info, so All is the "
                           "useful default here rather than error and warn.",
        },
        {
            # Custom with Loki's own values, not a Prometheus label_values. The same two
            # processes are named differently in the two systems: Prometheus scrapes
            # `api` and `cron`, Loki receives `server` and `cron`. A shared variable
            # would silently match nothing in Loki for the API. Also excluded: the
            # throwaway `ac`, `pos` and `smoke` job values left in Loki by push tests
            # during the rollout, which sit in the label index until their chunks age out.
            "name": "loki_job", "label": "Process (logs)", "type": "custom",
            "query": "server,cron",
            "options": [{"text": "server", "value": "server", "selected": False},
                        {"text": "cron", "value": "cron", "selected": False}],
            "multi": True, "includeAll": True, "allValue": "server|cron",
            "current": {"text": ["All"], "value": ["$__all"]},
            "description": "Loki's job names, which differ from Prometheus's for the same "
                           "processes: `server` here is the api job there. allValue is "
                           "server|cron rather than .* so the leftover ac/pos/smoke test "
                           "streams stay out of the log panels.",
        },
    ]},
    "annotations": {"list": []},
}

with open("grafana/dashboards/saas-product-analytics.json", "w") as f:
    json.dump(dashboard, f, indent=2)
    f.write("\n")

print("wrote grafana/dashboards/saas-product-analytics.json")
print("panels:", len([p for p in panels if p["type"] not in ("row",)]),
      "+", len([p for p in panels if p["type"] == "row"]), "rows")
print("blocked text panels:", len([p for p in panels if p["type"] == "text"]))
