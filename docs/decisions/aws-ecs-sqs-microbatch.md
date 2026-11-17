# ADR: AWS-only event-driven e-commerce analytics

**Original decision:** 2026-11-16. **Deployment scope updated:** 2026-11-17.

**Current decision:** AWS `eu-central-1` is the sole application and data-processing runtime. Shopify Development Store supplies Admin GraphQL customer operations and signed webhooks. ECS/Fargate hosts FastAPI and the background tasks; private Amazon RDS PostgreSQL holds operational state, customer mappings and the transactional inbox/outbox; SQS with a DLQ provides at-least-once asynchronous delivery; an idempotent ECS consumer persists events to S3; scheduled PySpark microbatch tasks on Fargate feed future Redshift Serverless/dbt Core analytics and Power BI.

The earlier option of a separate local Kubernetes/Kafka/Spark/PostgreSQL runtime is superseded. Developer workstations remain suitable for code editing, tests, notebooks, Git, AWS CLI and Terraform, but they are not a parallel environment. Spark `local[*]` describes execution inside the AWS Fargate container, not a local workstation deployment.

The dedicated AWS VPC uses two public subnets with an Internet Gateway and optional public HTTPS ALB, two private application subnets with AZ-local NAT gateways for outbound access, and two isolated RDS subnets without internet default routes. The database must never be publicly accessible. Customer synchronization is an on-demand ECS/Fargate workload, not a laptop-to-RDS application dependency.

**Reasoning:** A single AWS runtime avoids the extra infrastructure, synchronization and operations burden of maintaining a second transport and database stack. SQS is not a persistent Kafka event log: durable S3 objects are the replay source. Once-daily PySpark microbatches are not continuous Spark Structured Streaming. The live webhook ingestion and durable SQS-to-S3 landing path runs independently of the daily analytics cadence.

**Verified foundation (2026-11-17):** The Terraform-managed AWS VPC and private RDS, ECS cluster, ECR repositories, S3 landing bucket, SQS and DLQ exist. RDS is available and private; ECS has no active task definitions, running tasks or services. Redshift Serverless workgroups and Step Functions state machines are not deployed. The Shopify shop, custom-ID metafield and installed app scopes passed a read-only Admin GraphQL verification. The committed customer-sync foundation has not been deployed or executed against RDS/Shopify writes.

**Deployment gates:** Prepare versioned RDS migrations, least-privilege database roles and secrets, verified TLS, a durable Shopify reconciliation/attempt ledger and an ECS worker image. Validate single-customer create/find/retry and automated idempotency/recovery before bulk 5,942-customer synchronization. Require functional images, webhook HMAC validation, encrypted storage, cost review and infrastructure approval before enabling ongoing services. Code or Terraform commits do not establish production readiness.
