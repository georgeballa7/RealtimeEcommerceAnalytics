# Real-Time E-commerce Analytics

Layered Shopify-first e-commerce engineering and customer-retention analytics.

## Data layers

Immutable Online Retail II historical Bronze data is stored on S3. Silver contains canonical customer, product, variant, order and item records; missing customer IDs are quarantined and cross-sheet duplicate lines documented. All 5,942 identifiable customers remain in scope for later Shopify sync. Gold analytical models are planned.

## Architecture

Local development: Shopify development store; FastAPI/services on Kubernetes; PostgreSQL, Kafka and Spark Structured Streaming (planned).

AWS design: Shopify webhook → ALB → ECS Fargate API → RDS PostgreSQL inbox/outbox → ECS publisher → SQS/DLQ → ECS consumer → S3 event landing → scheduled ECS PySpark microbatches → Redshift Serverless / dbt / Power BI (planned).

Terraform has provisioned the AWS foundation, but not all application services are operational. See [architecture decision](docs/decisions/aws-ecs-sqs-microbatch.md).

## Workflow

Run the historical Bronze ingestion, Silver transformation and read-only Silver business audit using entrypoints in [scripts/README.md](scripts/README.md). The Python implementation lives in `src/ecommerce/pipelines/bronze/` and `src/ecommerce/pipelines/silver/`.

Other concerns remain separate: `src/ecommerce/domain/`, `application/`, `infrastructure/`, and `interfaces/`; `apps/` for deployables; `infrastructure/bootstrap/` for remote Terraform state and `infrastructure/terraform/` for AWS resources; `analytics/`, `ml/`, `notebooks/`, `data/schemas/`, and `tests/` for their respective purposes.
