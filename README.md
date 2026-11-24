# Real-Time E-commerce Analytics

How do you turn repeated, occasionally out-of-order Shopify notifications into trustworthy sales and retention reports? This AWS project captures signed webhooks while PostgreSQL can be stopped, projects them into operational records, and publishes reconciled snapshots to a private Redshift Serverless warehouse. Tableau users can follow an order from its source through a sales fact or examine a customer's estimated risk of placing no orders in the next 30 days.

![End-to-end architecture from Shopify to Tableau](docs/architecture/diagrams/exports/overview.svg)

## What the platform produces

| Responsibility | Implementation | Result |
| --- | --- | --- |
| Transaction simulation | Historical Online Retail II calibration, Shopify development store, scheduled ECS tasks | Reproducible historical backfill and distinct live synthetic orders |
| Event capture | API Gateway, FastAPI on Lambda, SQS, S3, DynamoDB | Signed deliveries archived independently of RDS availability |
| Operational processing | ECS on Fargate, RDS PostgreSQL | Deduplicated order, line and refund projections |
| Warehouse refresh | AWS Glue, Amazon S3, Redshift Data API | Validated Silver snapshots and a manifest load ledger |
| Analytics | dbt Core on Fargate, Redshift SQL | Sales dimensions and facts, daily sales, churn features and a categorical Naive Bayes baseline |
| Reporting | Tableau Desktop, Systems Manager port forwarding | Read-only access to sales and churn views |

## Explore the project

1. [Architecture and design decisions](docs/architecture/README.md) explains network paths, ownership and why the system separates capture, projection and analysis.
2. [Pipeline guide](docs/pipelines/README.md) follows a delivery and a scheduled processing slot through success, retry and reconciliation.
3. [Data model and lineage](docs/data-model/README.md) covers PostgreSQL constraints, analytical grains and metric interpretation.
4. [Operations and recovery](docs/operations/README.md) covers deployment boundaries, verification, failure diagnosis and shutdown.
5. [Tableau connection guide](docs/tableau_redshift_connectivity.md) and [SQL model evaluation](docs/churn_sql_model_v1.md) explain how to consume the results. The workbook is managed separately.
6. [Documentation validation](docs/validation.md) records the evidence and checks behind these guides.

## Implementation status

The November 21 checkpoint recorded a successful Glue → Redshift → dbt run. The November 24 infrastructure record confirms retirement of 23 legacy resources and cleanup of obsolete Terraform outputs. These dated observations answer different questions. To establish freshness for a reporting day, match its slot marker, manifest, warehouse acknowledgement and dbt exit status in the operations guide.

## Start locally

From the repository root with Python 3.12, install the development dependencies and run the same application checks as Python CI. No AWS deployment is required for these checks.

```sh
python -m pip install -e ".[dev,api,shopify,historical,postgres,aws]"
ruff check src tests
python -m pytest -q tests/unit
```

For historical ingestion, start with the [script guide](scripts/README.md). For the deployed platform, start with [read-only verification](docs/operations/README.md). Local unit tests use fixtures and mocks. Live ingestion creates transactions in the configured development store.
