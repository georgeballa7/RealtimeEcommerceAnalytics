# RealtimeEcommerceAnalytics — live AWS infrastructure

**Environment:** `eu-central-1`, AWS account `825414820081`. Managed Terraform state is in the versioned S3 bucket `realtime-ecommerce-tfstate-eb4a631483cb` under `ecommerce/demo/terraform.tfstate` with native lockfiles. Do not commit state, local `backend.tf`, credentials, full `tfvars` or plan artifacts.

## Deployed network (21 November 2026)

The former continuously running shared NAT Gateway (`nat-00f95208151412909`) was **deleted via Terraform** on 21 November 2026. Terraform also removed its two application subnet `0.0.0.0/0` routes, preserving both S3 Gateway Endpoint routes. The current desired `nat_gateway_mode` is **`on_demand`**.

Terraform continues managing the VPC, two public subnets, two private application subnets, two isolated database subnets, the Internet Gateway, reserved public IPv4 EIP (`eipalloc-03c5d096fcb30e03a`), route tables, NAT lifecycle IAM, Lambda, CloudWatch log group and EventBridge Scheduler. **Terraform must not own the ephemeral NAT gateway or its private application default routes** in on-demand mode.

The on-demand NAT controller prewarms at **07:30** and **15:30 Europe/Berlin** for the 08:00/16:00 generators. The lifecycle runs at **16:30**; NAT is deleted after verified completion and quiet-period checks, potentially before the 18:00 operational target. The controller requires its DynamoDB lock, validates queue/worker/Glue activity before NAT deletion, and is intended to leave **no NAT** running outside required processing windows.

At cutover, the old NAT was deleted, both application default routes were absent, and the reserved EIP was unassociated. A live Lambda invocation at 09:47 Berlin time returned `missed_prewarm_window`, the correct outcome outside the eligible creation interval. **The next 15:30 prewarm and resulting 16:00 Shopify connectivity must still be observed to validate the complete lifecycle.**

A CloudWatch alarm `ecommerce-demo-nat-lifecycle-errors` alerts the confirmed infrastructure SNS mailbox when the controller fails or the outbound routes are not active during the 07:50, 15:50 or 16:20 preflight checks. The readiness monitor does not itself create AWS resources; the lifecycle controller already handles idempotent provisioning and recovery.

A NAT Gateway incurs charges while provisioned; the *reserved public IPv4 address may still incur hourly charges when unattached*. NAT creation/deletion also has lead time, so a running ECS task cannot assume continuous internet egress outside the scheduled windows.

## Additional deployed components

- Private RDS PostgreSQL, ECS/Fargate tasks and ECR repositories, SQS/DLQ, DynamoDB, S3 landing and replay data, CloudWatch and the Phase 5 API/Lambda pipeline.
- Glue Silver processing and Redshift Serverless warehouse, with a pinned dbt ECS task definition.
- Phase 6 coordinator/loader, Phase 7 dbt orchestration and Tableau SSM access are deployed as **existing CloudFormation stacks**. Guarded Terraform import declarations are staged in `existing_stacks_import.tf`; do not apply them without reviewing the complete live-state plan. Their CloudFormation templates remain version-controlled.
- The RDS instance can be stopped operationally. Its temporary stopped state is not a desired Terraform resource deletion.

## Planning and operational safeguards

The `terraform.tfvars.example` captures only basic non-secret settings. **It is not sufficient for a full plan of the deployed account.** Many Phase 5/6 components are opt-in, and their existing task images must remain pinned to the live immutable ECR digests. Reconstruct those effective inputs from the protected state, including optional RDS bootstrap/customer sync/catalog sync, Phase 5 Lambda/generator, Redshift, Glue and dbt.

```powershell
cd infrastructure/terraform
# Configure ignored backend.tf for the protected S3 backend; set AWS profile.
terraform init -input=false
terraform fmt -check
terraform validate
# Use the reviewed effective deployment tfvars, not the example alone.
terraform plan -var-file=effective-deployment.auto.tfvars.json -out=reviewed.tfplan
# Inspect every change before using terraform apply reviewed.tfplan.
```

Never run a full `terraform apply` with default/partial image variables against the live deployment. Check for changes to existing task definitions, RDS, Redshift, VPC, Shopify schedules, Glue jobs and CloudFormation-managed resources; stop if not expected.

The previous `scripts/aws/plan_phase7_nat_cutover.ps1` and `apply_phase7_nat_cutover.ps1` were **one-time, static-to-on-demand migration tools**. They are no longer appropriate after the completed cutover and must not be re-run.

To roll back in a verified quiet window, set `nat_gateway_mode = "static"` and `stage_nat_lifecycle = true` with all other **deployed** input variables pinned, review the exact Terraform plan, and ensure the controller is disabled while the Terraform-owned static NAT and routes are restored. Preserve the original EIP.

## Cost control and security

Keep the AWS-account budget alerts separate from any third-party training subscription charges. Budgets are notifications, not hard spending limits; monitor EIP, NAT hours, Redshift compute/storage, RDS, Fargate, logs and data transfer. S3 Gateway Endpoint access must remain private and avoid unnecessary NAT data charges. The project is a cost-controlled learning environment, not a production high-availability configuration; review Multi-AZ, monitoring, alerting, security, and operational recovery before production use.

### Current-state variable reconstruction

The `scripts/aws/phase7_nat_cutover_plan.py snapshot` helper now supports both the historical static deployment and today's on-demand state. It produces the same pinned image/feature-flag inputs for future full plans while refusing partially migrated NAT states. Run it on `terraform show -json` output in a secure temporary directory; write the resulting file to an ignored `*.auto.tfvars.json` path. Treat the output as sensitive deployment configuration, never commit it, and check current AWS values before applying.
