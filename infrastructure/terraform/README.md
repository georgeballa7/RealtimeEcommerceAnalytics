# E-commerce AWS infrastructure (proposal)

This Terraform configuration targets **eu-central-1** with a dedicated VPC, public HTTPS ALB, ECS/Fargate application tasks in private application subnets, a private RDS PostgreSQL instance, encrypted SQS queue and DLQ, private S3, and scheduled PySpark (single-container microbatch).

Network: two public subnets with Internet Gateway, two application subnets and **one shared NAT Gateway by default in the development environment** (configurable to two for per-AZ egress resilience), and two isolated RDS subnets **without any internet default route**. Both private application subnet default routes use NAT 1 when `nat_gateway_count = 1`. This saves one NAT Gateway's standing hourly charge but introduces a single-AZ egress dependency and possible cross-AZ data-transfer charges. RDS accepts TCP 5432 only from the ECS application security group.

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

Review the account, resource counts, region, security rules and spend before applying. NAT Gateways, public IPv4 addresses, RDS and potential ALB/Fargate tasks are billable even when idle. For an existing two-NAT deployment, a reviewed Terraform apply should replace application subnet 2's default route with NAT 1 and remove NAT 2 and its allocated public IPv4 address; do not delete NAT manually before route migration. AWS Budget alerts are not spend limits. Terraform state contains sensitive information: keep it out of Git and configure protected state and locking before collaborative or CI-managed apply.

## Reliability and security

The AWS deployment uses SQS standard queue plus DLQ and S3 as the durable replay source. Consumer code must write idempotently to S3 *before* acknowledging SQS. Spark runs scheduled microbatches using PySpark local mode, **not** continuous distributed Structured Streaming. Add watermarking, non-overlap protection, observable metrics and alarms before enabling automated runs. A real customer deployment would require capacity testing, Multi-AZ RDS, multiple healthy API tasks, dedicated app/database roles, WAF and recovery validation. Never use the RDS master account for the API. RDS deletion protection is enabled and a final snapshot is configured.

Nothing is automatically deployed from this PR; Redshift, completed apps, Shopify integration and Power BI are separate outstanding implementation work.

## Terraform state and operational safeguards

Provision the independent S3 remote-state bucket from `../bootstrap/` first. Then copy `backend.tf.example` to the ignored local `backend.tf`, replace the placeholder and reinitialize Terraform. The bootstrap root retains a securely backed-up local state. Native S3 lockfiles require Terraform 1.10 or later; never commit state or plan artifacts.

The application database secret is created **without a secret value**. Populate it securely and create a matching least-privilege PostgreSQL user before enabling API tasks; the API execution role can only retrieve this secret. CloudWatch alarms monitor SQS DLQ messages, event backlog age and RDS CPU and send to an SNS topic; the configured mailbox is `george.data.alerts@gmail.com` and the recipient **must confirm** the AWS SNS subscription email before notifications arrive. Confirm CloudWatch RDS CPU metrics after applying the alarm-dimension fix. Finish integration testing, broader monitoring and IAM review before deployment.

## Cost-control decision (November 2026)

The personal spending envelope is **$75 per month including a $34 AWS Skill Builder subscription charged directly to a bank account**. AWS Budgets **cannot include that separate subscription**. The equivalent AWS-account spending ceiling is **$41 per month**, and an AWS Budget alert is a notification, not a hard stop. Review actual usage and remaining credits in Billing; Cost Explorer can lag and promotional credits can make costs appear negligible.

Changing this repository **does not modify running NAT Gateways** until Terraform is applied from the protected state root. Before apply, verify the plan shows removal of only NAT Gateway index 1, its Elastic IP index 1 and a route update for application route table index 1; ensure the RDS database, S3, and private subnet layout are preserved. After apply, inspect both application routes and test a one-off ECS task's outbound Shopify connectivity. Retain the S3 Gateway Endpoint to avoid unnecessary NAT S3 traffic.
