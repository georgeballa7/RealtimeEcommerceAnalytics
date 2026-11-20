-- Run once under the designated dbt deployment database identity.
-- This prepares ONLY the dbt-managed namespace; the 2026 Silver snapshots
-- and historical `silver_staging` data are intentionally untouched.
-- The dbt profile's default schema is analytics and custom schema suffixes
-- are staging / marts (default dbt generate_schema_name behavior).
--
-- Precondition: verify the executing principal is the intended dbt runtime
-- and can SELECT the required `silver_staging` source tables.
-- Do NOT run with PUBLIC schema ownership/grants or a general admin token.

CREATE SCHEMA IF NOT EXISTS analytics_staging;
CREATE SCHEMA IF NOT EXISTS analytics_marts;

-- dbt owns/manages its views/tables in these two schemas after its first run.
-- Restrict CREATE/USAGE to the dbt runtime role through approved IAM auth.
-- Use a separate read-only role for downstream analytics and Power BI.
