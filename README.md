# Real-Time E-commerce Analytics

Layered Shopify-first e-commerce engineering and customer-retention analytics.

## Data layers

Immutable Online Retail II historical Bronze data is stored on S3. Silver contains canonical customer, product, variant, order and item records; missing customer IDs are quarantined and cross-sheet duplicate lines documented. All 5,942 identifiable customers remain in scope for later Shopify sync. Gold analytical models are planned.

## Architecture

AWS is the sole application runtime in `eu-central-1`. The Shopify development store is the external source; developer machines are used for code, notebooks, tests, Git, Terraform and AWS tooling, not for a second deployed Kubernetes/Kafka/PostgreSQL/Spark stack.

**Target event path:** Shopify signed webhooks → public HTTPS ALB → private ECS/Fargate FastAPI → RDS PostgreSQL inbox/outbox → ECS outbox publisher → SQS/DLQ → ECS consumer → durable S3 event landing → daily PySpark microbatch on ECS/Fargate → Redshift Serverless → dbt Core → Power BI. Customer identity synchronization runs as an on-demand ECS/Fargate task. Historical 2026 synthetic backfill is stored separately on S3 with explicit provenance; customer churn modeling with MLflow is planned.

The Terraform-managed AWS foundation is provisioned, including the private RDS instance, ECS cluster, ECR repositories, SQS/DLQ and S3. As of 17 November 2026, no ECS tasks/services or active task definitions are running, and Redshift Serverless and Step Functions are not deployed. This infrastructure status does not imply end-to-end pipeline readiness. See the [architecture decision](docs/decisions/aws-ecs-sqs-microbatch.md).

## Workflow

Run the historical Bronze ingestion, Silver transformation and read-only Silver business audit using entrypoints in [scripts/README.md](scripts/README.md). The Python implementation lives in `src/ecommerce/pipelines/bronze/` and `src/ecommerce/pipelines/silver/`. The Shopify customer identity foundation and current deployment prerequisites are described in [README_IMPLEMENTATION.md](README_IMPLEMENTATION.md).

Other concerns remain separate: `src/ecommerce/domain/`, `application/`, `infrastructure/`, and `interfaces/`; `apps/` for deployables; `infrastructure/bootstrap/` for remote Terraform state and `infrastructure/terraform/` for AWS resources; `analytics/`, `ml/`, `notebooks/`, `data/schemas/`, and `tests/` for their respective purposes.
