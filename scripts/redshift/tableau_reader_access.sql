-- Redshift Tableau reader access. Requires the existing tableau_analytics_reader role.
GRANT USAGE ON SCHEMA analytics TO ROLE tableau_analytics_reader;
GRANT USAGE ON SCHEMA analytics_marts TO ROLE tableau_analytics_reader;
GRANT USAGE ON SCHEMA analytics_ml TO ROLE tableau_analytics_reader;
GRANT USAGE ON SCHEMA analytics_staging TO ROLE tableau_analytics_reader;
GRANT USAGE ON SCHEMA silver_staging TO ROLE tableau_analytics_reader;
GRANT USAGE ON SCHEMA public TO ROLE tableau_analytics_reader;

GRANT SELECT ON ALL TABLES IN SCHEMA analytics TO ROLE tableau_analytics_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics_marts TO ROLE tableau_analytics_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics_ml TO ROLE tableau_analytics_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA analytics_staging TO ROLE tableau_analytics_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA silver_staging TO ROLE tableau_analytics_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO ROLE tableau_analytics_reader;

GRANT SELECT FOR TABLES IN SCHEMA analytics TO ROLE tableau_analytics_reader;
GRANT SELECT FOR TABLES IN SCHEMA analytics_marts TO ROLE tableau_analytics_reader;
GRANT SELECT FOR TABLES IN SCHEMA analytics_ml TO ROLE tableau_analytics_reader;
GRANT SELECT FOR TABLES IN SCHEMA analytics_staging TO ROLE tableau_analytics_reader;
GRANT SELECT FOR TABLES IN SCHEMA silver_staging TO ROLE tableau_analytics_reader;
GRANT SELECT FOR TABLES IN SCHEMA public TO ROLE tableau_analytics_reader;

ALTER DEFAULT PRIVILEGES FOR USER "IAMR:ecommerce-phase6-dbt-task-demo"
IN SCHEMA analytics_marts
GRANT SELECT ON TABLES TO ROLE tableau_analytics_reader;

ALTER USER "IAMR:ecommerce-tableau-bi-readonly-demo" SESSION TIMEOUT 86400;
