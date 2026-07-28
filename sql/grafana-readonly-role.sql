-- Read-only role for the Grafana PostgreSQL datasource.
--
-- Run this with an account that holds CREATE ROLE. The application's own user
-- (postgresec2) does not: it was tried and reported rolcreaterole = false, so it
-- cannot be used here.
--
-- Least privilege on purpose. A Grafana Postgres datasource executes arbitrary
-- SQL as whatever role it is given, so the role is the only real containment:
-- it may connect and read exactly the four tables the product dashboards query,
-- and nothing else. Deliberately not the application's user, so a query typed
-- into Grafana cannot write and cannot reach a table nobody decided to expose.
--
-- Replace <STRONG_PASSWORD> before running, then put the same value in
-- secrets/grafana-postgres.env on the observability host. Do not commit it:
-- secrets/ is gitignored.

CREATE ROLE grafana_ro WITH
  LOGIN
  PASSWORD '<STRONG_PASSWORD>'
  NOSUPERUSER
  NOCREATEDB
  NOCREATEROLE
  NOINHERIT
  -- The datasource opens a pool per dashboard refresh; five is enough for the
  -- two product dashboards and low enough that a runaway panel cannot exhaust
  -- the database's connection slots.
  CONNECTION LIMIT 5;

GRANT CONNECT ON DATABASE performance_tracker TO grafana_ro;
GRANT USAGE ON SCHEMA public TO grafana_ro;

-- Exactly the tables the dashboards read. Adding a panel that needs another
-- table means adding a GRANT here, which is the review point.
GRANT SELECT ON action_logs TO grafana_ro;
GRANT SELECT ON "Users"     TO grafana_ro;
GRANT SELECT ON "Tenants"   TO grafana_ro;
GRANT SELECT ON companies   TO grafana_ro;

-- Verify what the role can actually reach:
--   SELECT table_name, privilege_type FROM information_schema.table_privileges
--    WHERE grantee = 'grafana_ro' ORDER BY table_name;
