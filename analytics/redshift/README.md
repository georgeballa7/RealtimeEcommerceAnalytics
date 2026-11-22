# Redshift, dbt and churn analytics

The AWS deployment uses a **private Amazon Redshift Serverless** workgroup and database `analytics`. Glue extracts private RDS data into versioned Bronze/Silver S3 snapshots, validates records, and publishes committed Silver manifests before Redshift loading.

| Schema | Purpose |
| --- | --- |
| `silver_staging` | Reconciled historical synthetic and Shopify operational source tables |
| `analytics_staging` | dbt staging views |
| `analytics_marts` | Dimensions, order/refund/cancellation facts, sales and churn feature marts |
| `analytics_ml` | SQL Naive Bayes churn model and evaluation |
| `analytics_bi` | Curated sales/churn views for Tableau |

**Last verified evening run:** Glue, Redshift and dbt completed successfully on 21 November 2026. This is a checkpoint, not a real-time health assertion. The source baseline includes 14,110 synthetic orders; Shopify order counts evolve with each successful daily batch.

The workgroup is private and cost-controlled. dbt runs as an on-demand ECS/Fargate task with an orchestrator, not an always-on service. Tableau Desktop uses read-only database privileges and a temporary SSM tunnel. Model scoring is a learning baseline, not production churn intervention.

**Reproducibility:** `analytics/dbt/` contains model definitions and data tests; `scripts/redshift/` contains schema, read grants, BI views and churn SQL. `infrastructure/terraform/` defines core infrastructure; existing CloudFormation stacks have guarded import declarations, but state reconciliation still requires an inspected plan. Never apply example/default deployment variables to the running account.
