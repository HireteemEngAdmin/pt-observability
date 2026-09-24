"""Generate the SaaS Product Analytics: Users dashboard.

Run from the repo root:
  python3 grafana/dashboards/build-saas-product-analytics-users.py

This is the RESTRICTED half of a pair. Its companion,
build-saas-product-analytics.py (uid pt-saas-analytics), holds everything
aggregate: rates, counts, per-tenant totals, retention cohorts. This file holds
the two panels that name people.

Why they are separate files at all, since they answer neighbouring questions:
Grafana grants dashboard permissions per dashboard, not per panel. A single
dashboard mixing a login-rate graph with a column of email addresses can only be
permissioned as though all of it were email addresses, so either the product
numbers get locked away from the people who need them or the user list gets
opened to everyone who wants a graph. Splitting them is the only way to have
both. Restrict this one; share the other.

What is on it and what that means:

- Every row carries a full name and an email address. Both tables also expose the
  tenant, so this dashboard discloses which company a named individual works
  for. That is the reason for the permission, not the row count.
- Both queries read from a role granted SELECT on four tables and nothing else
  (sql/grafana-readonly-role.sql): action_logs, "Users", "Tenants", companies.
- Neither query has ever been executed. The datasource is provisioned
  (grafana/provisioning/datasources/postgres.yml, uid postgres) but the SSH
  tunnel it depends on is not up: grafana-pg-tunnel.service reports inactive and
  172.17.0.1:5432 is closed. Schema comes from reading the Sequelize models in
  performance-tracker backend/models/, so the column names are as good as that
  source and no better.

Query constraints, the same ones the aggregate dashboard works under:

- action_logs is 2.1M rows and 309 MB from 2024-03-05, and its only created_at
  indexes are partial (method IN ('ACTIVATED','DEACTIVATED')), so they do not
  serve activity queries. The leaderboard is therefore bounded by the dashboard
  time range through $__timeFilter and is a filtered scan. The inactive list
  reads "Users".last_login instead, which is 5384 rows and needs no bound.
- "Active" means logged in (action_logs.route = '/login') or performed an
  authenticated POST/PATCH/PUT/DELETE. Successful reads are recorded nowhere.
  That asymmetry is why the two tables can disagree about the same person, and
  the descriptions say so.
- Test accounts excluded: email LIKE 'mocktm_%' (5 rows) and email LIKE '%demo%'
  (9 rows), and nothing else. NOT @hireteem.com or @getreach.com: those are the
  Team Members, who are the real users and the bulk of the activity.
- "Users" and "Tenants" were created by Sequelize with capitals and camelCase
  columns, so they need double quotes; action_logs and companies are
  lower_snake. Postgres folds unquoted identifiers to lower case.

There is deliberately no free-text search variable. A textbox interpolated into
rawSql is a string concatenated into a query, and while the role is read-only and
limited to four tables, the table panels already filter and sort client-side
(filterable: true), so the injection surface buys nothing.
"""
import json

PG = {"type": "postgres", "uid": "postgres"}

# See the note in the docstring: these two patterns and no others. The `_` in
# 'mocktm_%' is a LIKE wildcard rather than a literal, left unescaped on purpose so
# the exclusion reproduces exactly the 5 rows that were counted.
NO_TEST = """u.email NOT LIKE 'mocktm_%'
     AND u.email NOT LIKE '%demo%'"""

# Multi-value variables are interpolated with the sqlstring format, which quotes and
# escapes each value into 'a','b'. That only produces valid SQL if the variable can
# never hold Grafana's All sentinel, so these variables have includeAll off and every
# option selected by default instead. Same reason they are custom rather than query
# variables: a query variable against this datasource returns nothing until the tunnel
# is up, which would leave IN () and a syntax error rather than an empty panel.
#
# The `OR c.slug IS NULL` is not defensive padding, it is a bug fix. Both tables reach
# the company through LEFT JOINs so that a user with no TenantId still appears, but
# `c.slug IN ('teem','reach')` evaluates to NULL for those rows, and a NULL in WHERE
# discards them: the LEFT JOIN was being undone by the filter one line later. Caught by
# running the query against a recreated schema, where the tenant-less super_admin
# vanished from the leaderboard without any error. The 6 super_admins are exactly the
# rows this would have hidden.
COMPANY_IN = '(c.slug IN (${company_slug:sqlstring}) OR c.slug IS NULL)'
ROLE_IN = 'u.role IN (${user_role:sqlstring})'

PG_NOT_LIVE = ("Not yet executed: the PostgreSQL datasource is provisioned but its SSH "
               "tunnel is not up, so 172.17.0.1:5432 is closed. Column names come from the "
               "Sequelize models in performance-tracker backend/models/, not from the "
               "server. Expect to correct something the first time it runs.")

ACTIVITY_DEF = ("\"Active\" means logged in (action_logs.route = '/login') or performed an "
                "authenticated POST/PATCH/PUT/DELETE. Successful reads are recorded "
                "nowhere, so a user who only reads never appears as active.")

PII = ("This panel discloses names, email addresses and the company each person works "
       "for. It is on a separate dashboard from the product metrics so that this "
       "disclosure can be permissioned on its own.")

panels = []
pid = 0


def nid():
    global pid
    pid += 1
    return pid


def sql_targets(query, fmt="table"):
    """rawQuery:true is what makes Grafana run the SQL verbatim rather than trying to
    round-trip it through the visual query builder, which drops CTEs."""
    return [{"datasource": PG, "refId": "A", "rawQuery": True, "rawSql": query,
             "format": fmt, "editorMode": "code"}]


def row(title, y):
    return {"type": "row", "title": title, "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
            "id": nid(), "collapsed": False, "panels": []}


def table(title, query, x, y, w=24, h=14, desc="", overrides=None):
    return {
        "type": "table", "title": title, "description": desc, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y}, "datasource": PG,
        "targets": sql_targets(query, fmt="table"),
        # filterable and sortable client-side, which is what replaces a search box.
        "options": {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"], "countRows": False,
                               "fields": ""}},
        "fieldConfig": {"defaults": {
            "custom": {"align": "auto", "cellOptions": {"type": "auto"},
                       "inspect": False, "filterable": True},
        }, "overrides": overrides or []},
    }


def note(title, x, y, w, h, content):
    return {
        "type": "text", "title": title, "id": nid(),
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "options": {"mode": "markdown", "content": content},
        "transparent": False,
    }


# Right-align and colour the count columns so a leaderboard reads as one. Named
# overrides rather than a defaults change, because the text columns should stay left.
def numeric_override(field):
    return {"matcher": {"id": "byName", "options": field},
            "properties": [{"id": "custom.align", "value": "right"},
                           {"id": "custom.cellOptions",
                            "value": {"type": "color-text"}}]}


# ── What this dashboard is ────────────────────────────────────────
y = 0
panels.append(row("Scope", y))
y += 1
panels.append(note(
    "This dashboard names individuals", 0, y, 24, 6,
    "Both tables below carry **full names, email addresses and the company each person "
    "works for**. Restrict this dashboard's permissions accordingly.\n\n"
    "The aggregate product view - active users, logins, feature usage, tenants, retention "
    "cohorts - is on **SaaS Product Analytics** (`/d/pt-saas-analytics`), linked at the top "
    "of this page. Nothing on that dashboard identifies anyone, so it can be shared freely. "
    "This split exists because Grafana grants access per dashboard and not per panel: "
    "together in one dashboard, the product numbers would inherit this dashboard's "
    "restrictions.\n\n"
    "**Neither query here has run yet.** The PostgreSQL datasource is provisioned "
    "(uid `postgres`) but its SSH tunnel is not up, so the panels are empty and the SQL is "
    "unverified against the real schema. It was written from the Sequelize models in "
    "performance-tracker `backend/models/`.\n\n"
    "**Test accounts excluded** in both queries: `email LIKE 'mocktm_%'` and "
    "`email LIKE '%demo%'`. Deliberately *not* excluded: `@hireteem.com` and "
    "`@getreach.com`. Those are the Team Members. They are real users and most of the "
    "activity."))
y += 6

# ── Most active users ─────────────────────────────────────────────
panels.append(row("Most active users", y))
y += 1
panels.append(table(
    "Most active users in the selected range",
    # Grouped by u.id, not by email: id is the primary key, so the grouping is correct
    # even if two rows ever shared a display name. The other u.* columns are in the
    # GROUP BY because Postgres requires it unless it can prove functional dependence,
    # and it only does that for the whole table's key when selecting from that table
    # alone, not through this join.
    #
    # logins and mutations are split apart rather than reported as one "actions"
    # number, because they are the only two things action_logs records and they mean
    # different things: 40 logins is a client retry loop, 40 mutations is real use.
    f"""SELECT u.name                                                     AS "name",
       u.email                                                    AS "email",
       c.slug                                                     AS "company",
       u.role                                                     AS "role",
       count(*)                                                   AS "actions",
       count(*) FILTER (WHERE a.route = '/login')                  AS "logins",
       count(*) FILTER (WHERE a.route <> '/login')                 AS "mutations",
       count(DISTINCT date_trunc('day', a.created_at))             AS "active days",
       max(a.created_at)                                          AS "last seen"
  FROM action_logs a
  JOIN "Users"    u ON u.id = a.user_id
  LEFT JOIN "Tenants" t ON t.id = u."TenantId"
  LEFT JOIN companies c ON c.id = t."CompanyId"
 WHERE $__timeFilter(a.created_at)
   AND {NO_TEST}
   AND {COMPANY_IN}
   AND {ROLE_IN}
 GROUP BY u.id, u.name, u.email, c.slug, u.role
 ORDER BY count(*) DESC
 LIMIT 200""",
    0, y, w=24, h=16,
    desc="The 200 busiest users over the dashboard's time range, by rows written to "
         "action_logs. " + ACTIVITY_DEF + " So this ranks people by how much they change, "
         "not by how much they use: a heavy reader with no mutations does not appear at all "
         "beyond their login rows, and that is a property of the data rather than of the "
         "query. 'active days' counts distinct UTC days, because created_at is timestamptz "
         "and the session runs at the server's UTC while the dashboard renders in browser "
         "time. The company column can be empty, and those rows are always shown whatever "
         "the Company filter is set to: a user with no TenantId belongs to no company, and "
         "in practice that is the super_admins, who would otherwise be the most active "
         "accounts in the system and silently absent. Bounded by the time range "
         "because action_logs is 2.1M rows and 309 MB with no usable created_at index, so "
         "widening the range costs real time. " + PG_NOT_LIVE,
    overrides=[numeric_override(f) for f in
               ("actions", "logins", "mutations", "active days")]))
y += 16

# ── Inactive users ────────────────────────────────────────────────
panels.append(row("Inactive users", y))
y += 1
panels.append(table(
    "Users with no login for $inactive_days days",
    # Reads "Users".last_login rather than scanning action_logs: 5384 rows, no time
    # bound needed, and last_login is exactly the column the login path maintains.
    #
    # Deactivated users are kept and flagged rather than filtered out. They are not a
    # churn signal - they were switched off deliberately - but removing them makes the
    # list disagree with the aggregate counts on the other dashboard for no visible
    # reason, and someone auditing offboarding wants to see them.
    f"""SELECT u.name                                              AS "name",
       u.email                                             AS "email",
       c.slug                                              AS "company",
       u.role                                              AS "role",
       CASE WHEN u.last_login IS NULL THEN 'never logged in'
            ELSE 'gone quiet' END                          AS "kind",
       u."createdAt"                                       AS "signed up",
       u.last_login                                        AS "last login",
       CASE WHEN u.last_login IS NULL THEN NULL
            ELSE (now()::date - u.last_login::date) END    AS "days quiet",
       (u.deactivated_at IS NOT NULL)                      AS "deactivated"
  FROM "Users" u
  LEFT JOIN "Tenants" t ON t.id = u."TenantId"
  LEFT JOIN companies c ON c.id = t."CompanyId"
 WHERE {NO_TEST}
   AND {COMPANY_IN}
   AND {ROLE_IN}
   AND (u.last_login IS NULL
        OR u.last_login < now() - interval '1 day' * $inactive_days)
 ORDER BY u.last_login ASC NULLS FIRST
 LIMIT 500""",
    0, y, w=24, h=16,
    desc="Users who exist and are not logging in, ordered oldest first with the "
         "never-logged-in rows at the top. The kind column separates the two problems that "
         "share this symptom: 'never logged in' is an onboarding failure, 'gone quiet' is a "
         "churn signal, and they need different follow-up. Measured at the time of writing: "
         "2156 of 5384 users have never logged in and 1381 are deactivated. Deactivated "
         "users are flagged rather than excluded, because they are not churn and an "
         "offboarding audit wants them visible. Built on last_login rather than "
         "action_logs, which makes it cheap and also means it counts a login even if the "
         "user then only read, so this list and the leaderboard above can legitimately "
         "disagree about the same person. Ignores the dashboard time range by design: "
         "'quiet since' is relative to now, not to the selected window. " + PG_NOT_LIVE,
    overrides=[numeric_override("days quiet")]))
y += 16

dashboard = {
    "uid": "pt-saas-analytics-users",
    "title": "SaaS Product Analytics: Users",
    "description": "RESTRICTED. Named user lists for the Performance Tracker: the most "
                   "active users and the inactive users, with full names, email addresses "
                   "and company. Restrict this dashboard's permissions. The aggregate "
                   "product view is 'SaaS Product Analytics' (uid pt-saas-analytics), which "
                   "identifies nobody and can be shared freely; the two are separate "
                   "dashboards because Grafana grants access per dashboard rather than per "
                   "panel, so mixing them would force the product metrics behind this "
                   "restriction. Both queries read PostgreSQL through a read-only role "
                   "limited to four tables, and neither has been executed yet: the "
                   "datasource's SSH tunnel is not up.",
    "tags": ["performance-tracker", "observability", "product", "analytics", "pii",
             "restricted"],
    "links": [
        {"type": "link", "title": "Aggregate product analytics",
         "url": "/d/pt-saas-analytics/", "tooltip":
             "Active users, logins, feature usage, tenants and retention. Identifies "
             "nobody.",
         "icon": "external link", "targetBlank": False, "asDropdown": False,
         "includeVars": True, "keepTime": True, "tags": []},
    ],
    "timezone": "browser",
    "editable": True,
    "schemaVersion": 39,
    "version": 1,
    # No auto refresh. Every refresh is a filtered scan over a 309 MB table, and a
    # dashboard of named users left open on a screen is not something to poll.
    "refresh": "",
    "time": {"from": "now-30d", "to": "now"},
    "panels": panels,
    "templating": {"list": [
        {
            # Custom, and includeAll deliberately off with both values selected. With
            # ${...:sqlstring} interpolation, Grafana's All sentinel would be quoted as a
            # literal and IN ('$__all') would match nothing while looking valid. A query
            # variable has the same problem in reverse: it returns nothing until the
            # tunnel is up, which yields IN () and a syntax error.
            "name": "company_slug", "label": "Company", "type": "custom",
            "query": "teem,reach",
            "options": [{"text": "teem", "value": "teem", "selected": True},
                        {"text": "reach", "value": "reach", "selected": True}],
            "current": {"text": ["teem", "reach"], "value": ["teem", "reach"]},
            "multi": True, "includeAll": False,
            "description": "The two tenant companies. Interpolated with the sqlstring "
                           "format, which quotes and escapes each value, so this is a "
                           "value list and not concatenated user input.",
        },
        {
            "name": "user_role", "label": "User role", "type": "custom",
            "query": "super_admin,team_member,manager,representative",
            "options": [{"text": t, "value": t, "selected": True} for t in
                        ("super_admin", "team_member", "manager", "representative")],
            "current": {"text": ["super_admin", "team_member", "manager", "representative"],
                        "value": ["super_admin", "team_member", "manager",
                                  "representative"]},
            "multi": True, "includeAll": False,
            "description": "The four values of the role enum in "
                           "backend/models/user.model.js. Counts at the time of writing: "
                           "representative 2649, team_member 2642, manager 87, super_admin "
                           "6. Custom rather than a SELECT DISTINCT so the dropdown works "
                           "before the datasource is live, and the enum cannot change "
                           "without a migration anyway.",
        },
        {
            # Single-select on purpose: it is a threshold, not a set. Interpolated as a
            # bare number into interval '1 day' * $inactive_days, which avoids building
            # an interval out of string concatenation.
            "name": "inactive_days", "label": "Quiet for at least", "type": "custom",
            "query": "30,60,90,180,365",
            "options": [{"text": t, "value": t, "selected": t == "30"} for t in
                        ("30", "60", "90", "180", "365")],
            "current": {"text": "30", "value": "30"},
            "multi": False, "includeAll": False,
            "description": "Days since last login, as a threshold on the inactive-user "
                           "table. Never-logged-in users are always included, whatever this "
                           "is set to, since they have no last_login to compare.",
        },
    ]},
    "annotations": {"list": []},
}

with open("grafana/dashboards/saas-product-analytics-users.json", "w") as f:
    json.dump(dashboard, f, indent=2)
    f.write("\n")

print("wrote grafana/dashboards/saas-product-analytics-users.json")
print("panels:", len([p for p in panels if p["type"] != "row"]),
      "+", len([p for p in panels if p["type"] == "row"]), "rows")
