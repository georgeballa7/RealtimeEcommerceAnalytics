# Real-Time E-commerce Analytics

AWS-based Shopify order ingestion, operational processing, dimensional analytics, and customer-retention modeling. The development store generates **synthetic transactions**; historical Online Retail II data and the synthetic backfill have separate provenance.

## Implemented architecture

```text
EventBridge → private ECS generator / lifecycle → Shopify development store
Shopify webhooks → API Gateway → FastAPI Lambda → SQS / DLQ → landing Lambda
                                         → S3 event archive + DynamoDB index
S3 events → private ECS projector → private RDS PostgreSQL
Private RDS → AWS Glue (PySpark, private VPC connection) → S3 Bronze / Silver
            → Redshift Serverless (silver_staging) → dbt Core (ECS/Fargate)
            → analytics_staging / analytics_marts → analytics_ml / analytics_bi
            → Tableau Desktop (read-only, through SSM tunnel)
```

- **Operational:** Idempotent Shopify generation, signed webhook handling, event landing, queue redrive, private PostgreSQL state, and S3 replay records. DynamoDB coordinates processing and resource leases.
- **Warehouse:** Glue validates and reconciles operational extracts before the Redshift load. dbt builds three dimensions, four facts, daily sales, and customer-churn feature marts with data tests.
- **Churn:** SQL Naive Bayes baseline and read-only analytics views. Model output uses synthetic data and is **not** a production decision system.
- **Reporting:** Tableau reads Redshift schemas including `analytics_marts` and `analytics_bi`. **Power BI is not the selected BI tool for this project.**

### Network and operating schedule

The deployment is in `eu-central-1`: two public, two private application, and two isolated database subnets across two Availability Zones. ECS tasks and Glue's VPC connection use private application networking; RDS resides in the isolated DB subnet group. Redshift Serverless is private, with VPC network interfaces in the application subnets. API Gateway, Lambda, SQS, DynamoDB, EventBridge, S3, and Glue are regional managed services, not workloads inside those subnets.

| Europe/Berlin | Scheduled operation |
| --- | --- |
| 07:30 / 15:30 | On-demand NAT prewarm |
| 08:00 / 16:00 | Shopify order generation |
| 16:30 | Cancellation, refund, and lifecycle processing |
| After settled source events | Glue → Redshift → dbt |
| After successful completion | NAT deletion (not a fixed 18:00 shutdown) |

The end-of-day **18:00 target** is an operational objective, not a guarantee or an unconditional resource cutoff.

## Status and safety

The last independently verified evening checkpoint (**21 November 2026**) completed Glue, Redshift reconciliation, and dbt. At that checkpoint the warehouse held **14,110 historical synthetic orders and 446 live-generated Shopify orders**. The revised 16:30 lifecycle and NAT release logic have passed Lambda smoke tests; **the first full run on the new timetable and its cost/resource outcome still require verification**. Do not infer the current day's health from this checkpoint.

Terraform manages the core AWS estate. Some existing orchestration and Tableau-access resources are CloudFormation-managed; import declarations and matching templates are staged in this branch, **but live Terraform state parity and drift reconciliation are not yet certified**. Do not apply partial/default Terraform variables, import overlapping resources, or merge infrastructure changes without reviewing a complete non-destructive plan.

## Repository guide

| Location | Purpose |
| --- | --- |
| `src/ecommerce/`, `apps/` | Domain, application, interfaces, and AWS workload entrypoints |
| `scripts/`, `scripts/aws/`, `scripts/glue/` | Ingestion, guarded deployments, Glue, orchestration |
| `infrastructure/terraform/` | Core IaC, remote state references, staged imports |
| `infrastructure/cloudformation/` | Templates for existing separately owned stacks |
| `analytics/dbt/`, `analytics/redshift/`, `scripts/redshift/` | Warehouse models, SQL, quality tests, Tableau grants |
| `tests/` | Unit, integration, and guarded infrastructure checks |

For a local development environment:

```powershell
python -m pip install -e ".[dev,historical,aws]"
ruff check src apps tests
python -m pytest -q tests/unit
```

Follow [Terraform safeguards](infrastructure/terraform/README.md) before planning or changing AWS resources. Historical ingestion entrypoints are in [scripts/README.md](scripts/README.md). Project decisions, implementation checkpoints, operational evidence, and **architecture diagrams live in [Notion](https://app.notion.com/p/41f929a22c1581279669d9dca31d0e2d)**, not in GitHub diagrams or dated execution notes.

This is a cost-controlled engineering demonstration, not a production-HA or real-customer revenue system.
