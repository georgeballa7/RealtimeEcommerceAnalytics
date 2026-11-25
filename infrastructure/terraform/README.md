# RealtimeEcommerceAnalytics — live AWS infrastructure

**Environment:** `eu-central-1`, project AWS account. Managed Terraform state is in a versioned S3 backend under `ecommerce/demo/terraform.tfstate` with native lockfiles. Do not commit state, local `backend.tf`, credentials, full `tfvars` or plan artifacts.

[Network topology and ownership](../../docs/architecture/README.md) · [Runtime schedules and cost controls](../../docs/operations/README.md#availability-and-cost-controls)

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

### Current-state variable reconstruction

The `scripts/aws/phase7_nat_cutover_plan.py snapshot` helper now supports both the historical static deployment and today's on-demand state. It produces the same pinned image/feature-flag inputs for future full plans while refusing partially migrated NAT states. Run it on `terraform show -json` output in a secure temporary directory; write the resulting file to an ignored `*.auto.tfvars.json` path. Treat the output as sensitive deployment configuration, never commit it, and check current AWS values before applying.
