# E-commerce AWS infrastructure (proposal)

This Terraform configuration targets **eu-central-1** with a dedicated VPC, public HTTPS ALB, ECS/Fargate application tasks in private application subnets, a private RDS PostgreSQL instance, encrypted SQS queue and DLQ, private S3, and scheduled PySpark (single-container microbatch).

Network: two public subnets with Internet Gateway, two application subnets with one NAT Gateway per AZ, and two isolated RDS subnets **without any internet default route**. RDS accepts TCP 5432 only from the ECS application security group.

## Readiness

This is an **infrastructure foundation**. The repository still contains application scaffolding. The API is disabled until `api_image` and an issued regional ACM certificate are configured. The SQS consumer is disabled until `consumer_image` is supplied. Spark task definition is disabled until `spark_image` exists, and the schedule remains **DISABLED** even after the image is supplied. Images must be functional, tested, and use least-privilege application credentials and verified TLS. Production Shopify HMAC verification, idempotent transaction/outbox handling, database migrations, Redshift loads, and actual processing logic are not yet implemented.

## Review before applying

```powershell
cd infrastructure/terraform
Copy-Item terraform.tfvars.example terraform.tfvars
# Set expected account ID, check region and estimated costs
terraform init
terraform fmt -check
terraform validate
terraform plan -out=ecommerce.tfplan
```

Review the account, resource counts, region, security rules and spend before applying. Two NAT Gateways, Elastic IPs, RDS and potential ALB/Fargate tasks are billable even when idle. AWS Budget alerts are not spend limits. Terraform state contains sensitive information: keep it out of Git and configure protected state and locking before collaborative or CI-managed apply.

## Reliability and security

The AWS deployment uses SQS standard queue plus DLQ and S3 as the durable replay source. Consumer code must write idempotently to S3 *before* acknowledging SQS. Spark runs scheduled microbatches using PySpark local mode, **not** continuous distributed Structured Streaming. Add watermarking, non-overlap protection, observable metrics and alarms before enabling automated runs. A real customer deployment would require capacity testing, Multi-AZ RDS, multiple healthy API tasks, dedicated app/database roles, WAF and recovery validation. Never use the RDS master account for the API. RDS deletion protection is enabled and a final snapshot is configured.

Nothing is automatically deployed from this PR; Redshift, completed apps, Shopify integration and Power BI are separate outstanding implementation work.
