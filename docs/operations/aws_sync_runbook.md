# AWS synchronization: reproducible build and execution

Source of truth: Notion E-Commerce Project Plan and Engineering Runbook.
AWS region eu-central-1, account 825414820081. Working branch: feature/historical-data-foundation.

## Completed customer import — do not rerun

The full 5,942-customer synchronization has already finished successfully:
- ECS task: arn:aws:ecs:eu-central-1:825414820081:task/ecommerce-demo/525e5d354a2a485f9fe9dcc9848d69f5
- Task definition: ecommerce-customer-sync-demo:4
- Status STOPPED, exit code 0
- Application report: processed=5942, skipped=100, verified_new=5842
- PostgreSQL: 5942/5942 verified mappings
- Runtime image digest: sha256:2ab027aec8440819c6e638d9270effc663ef7125fa3c31decefab7b1f821e7ea

These facts describe the successful historical execution, NOT a run made using the new wrapper scripts.

## Reusable source-controlled components

- config/aws-demo.json: non-secret account, VPC network, task, ECR, bucket metadata
- Dockerfile.customer-sync: container image build recipe
- scripts/aws/publish_ecr_image.ps1: account guard, git revision, tests, Docker build, immutable tag, secure on-demand push, ECR digest verification
- scripts/aws/run_customer_sync.ps1: preview, active task family validation, concurrency guard, explicit write authorization, private ECS task launch and optional status check
- infrastructure/terraform/customer_sync.tf: declarative IAM/ECS task configuration
- scripts/sync_historical_customers.py: current sync algorithm and progress logging

The tools require Windows PowerShell, AWS CLI v2, configured AWS profile,
Docker Desktop, Python 3.12 with development dependencies and git.
They resolve the repository root from their script location.

## 1. Preview without AWS modifications

From the repository root:

    .\scripts\aws\publish_ecr_image.ps1
    .\scripts\aws\run_customer_sync.ps1

Both default to preview mode. Review account, cluster, subnet IDs, security
group, task definition, image target and intended count before further action.

## 2. Build and publish an immutable image (approval required)

Complete code review, tests, and commit all tracked source changes first.
Then (only when an actual new image is needed):

    .\scripts\aws\publish_ecr_image.ps1 -Execute -ConfirmPublish

This runs focused customer-sync tests, builds from Dockerfile.customer-sync,
tags with Git commit SHA, pushes to ECR and verifies the registry digest.
Do not confuse a local Docker image hash with the image digest reported by ECR.

### Docker Desktop/ECR authentication workaround (verified 17 Nov 2026)

Normal Docker login returned HTTP 400 despite valid credentials. The precise
Docker Desktop root cause remains unknown. The versioned publisher uses the
documented working procedure: retrieve a fresh ECR token using AWS CLI, write
a short-lived Docker config containing Base64-encoded AWS:token auth, invoke
docker --config <temporary directory> push, and delete the temporary config in
a finally block. Base64 does not encrypt secrets. Never display or commit this
file. Prefer a supported credential helper in future once verified working.

## 3. Review Terraform, never auto-apply

Update the customer_sync_image Terraform input with the registry digest URI.
Keep secret references intact. Review terraform fmt -check, terraform validate,
and terraform plan before explicitly approving any apply. Verify ECS task
definition ACTIVE and image digest after apply. A successful Docker push
does not update ECS or trigger Shopify operations.

## 4. Run a controlled ECS task when authorized

Default preview:

    .\scripts\aws\run_customer_sync.ps1 -Limit 5

Read-only AWS execution:

    .\scripts\aws\run_customer_sync.ps1 -Execute -Limit 5

Write-enabled future execution requires -Execute -AllowWrites plus interactive
confirmation. It is NOT required for the completed customer import.
The launcher refuses concurrent same-family ECS jobs. Its temporary JSON
network and override files are deleted. It does not retry a failed launch.
For long-running jobs omit -Wait and monitor the ECS task independently.

## 5. Verify and capture evidence

Record the task ARN and confirm ECS STOPPED and container ExitCode 0.
Read CloudWatch /ecs/ecommerce-demo/customer-sync: final customer mapping count
must equal the validated Silver manifest count. Store sanitized run evidence in
Notion; do not store customer payloads, credentials or raw tokens.

On any failure, inspect logs and the PostgreSQL attempt ledger, reconcile
remote Shopify identities and existing mappings before authorizing reruns.

## Next engineering refactor (pending)

Extract the existing customer CLI into reusable configuration, dataset loading,
service wiring, runner and reporting modules; replace hardcoded 5942 constraints
with manifest-derived acceptance; add structured logging and bounded recovery
tests. Preserve the successful Shopify custom-ID verification and durable lease
semantics. Reuse this framework for complete eligible product/variant mapping.
Do not push all historical orders to Shopify: full history remains S3 to Redshift.
