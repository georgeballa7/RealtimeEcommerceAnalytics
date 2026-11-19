# Phase 5 AWS execution record — PowerShell workflows

All commands run from the repository root in Windows PowerShell; all existing
credentials remain in Secrets Manager. AWS profile: `ecommerce-admin`.
Account: `825414820081`, region: `eu-central-1`.

## Actual migration operation (executed November 19, 2026)

- Immutable migration image:
  `ecommerce-phase5-migration-demo@sha256:359e17670380993c89cff1868a00e44774f121b85929fdba1230ff20cf41b5f0`
- ECS task definition: `ecommerce-phase5-migration-demo:1`.
- Private subnets: `subnet-0248e35f98ea3fbf6`,
  `subnet-0481cb476d153d5ba`.
- Existing ECS security group: `sg-0128bc9b086b7072b`; public IP disabled.
- Preflight task ID: `b34f57cee80d4588ad8a18f5ad8fcac0`,
  CloudWatch: `Phase 5 migration 007: pending; applied=False`.
- Apply task ID: `0744dab4a2924886b412c13e900e600c`,
  CloudWatch: `Phase 5 migration 007: applied; applied=True`, exit code 0.
- No bootstrap rerun, no password rotation. SQL 007 and app-role grants applied.

The exact AWS actions were DescribeTaskDefinition, ListTasks, RunTask,
DescribeTasks and CloudWatch GetLogEvents. They are recorded as executable
scripts in `scripts/aws/phase5_aws_common.ps1` and
`scripts/aws/run_phase5_migration.ps1`.

**Do not rerun the migration just for documentation; it has already succeeded.**

## Reproducible Windows commands

```powershell
aws sts get-caller-identity --profile ecommerce-admin
# Existing Docker credential-helper workaround (no docker login):
.\scripts\aws\publish_ecr_image.ps1 -Workload phase5_pricing
.\scripts\aws\publish_ecr_image.ps1 -Workload phase5_pricing -Execute -ConfirmPublish
# Preview price task registration:
.\scripts\aws\register_phase5_pricing.ps1 -ImageUri $pricingImage
# Apply the saved Terraform plan from the wrapper only when reviewed:
.\scripts\aws\register_phase5_pricing.ps1 -ImageUri $pricingImage -Execute
# Preview ECS task (no launch):
.\scripts\aws\run_phase5_pricing.ps1 -ImageUri $pricingImage
# Full 3408-variant Shopify read-only comparison:
.\scripts\aws\run_phase5_pricing.ps1 -ImageUri $pricingImage -Execute
# All 3408 Shopify/RDS updates (explicit confirmation required):
.\scripts\aws\run_phase5_pricing.ps1 -ImageUri $pricingImage -Execute -Apply
```

The migration runner also supports `-Execute` (read-only preflight) and
`-Execute -Apply`, but migration 007 is already completed.

All ECS launchers verify account, pinned image digest and family, prevent
concurrent same-family launches, use the established private network,
wait for completion and retrieve CloudWatch logs. Temporary JSON files
contain only networking and command overrides, not credentials.

The previous pricing image digest was registered but failed manifest checksum validation. Replace it with the new verified digest in the same ECS task family before retrying.

**Important:** The `phase5_migration_image` variable was used when the
existing migration task definition was registered. Preserve that value
for subsequent full Terraform plans, or Terraform may propose deleting
the migration task definition. The registration wrapper supplies BOTH the pricing and migration image
variables to prevent the migration task definition from being removed.
Review the plan for unintended changes or deletions before applying.

## 2026-11-19 first pricing execution — checksum preflight rejection

- Task ID: `d9b47b6f9ec647e993d66fcde6652331`, exit code **2**.
- Command: full `--apply --limit 3408` with pinned manifest confirmation.
- Result: `Manifest checksum differs from approved Phase 5 release`.
- The script rejected the manifest **before opening Shopify OAuth or database connections**, so it performed **zero Shopify and RDS writes**.
- Root cause: Windows checkout line-ending conversion is the leading explanation; the repository did not specify `eol=lf` for the SHA-pinned JSON and Docker copied the checkout into the image. The updated image build normalizes CRLF to LF and verifies the exact approved checksum before publication.
- Do not reuse image digest `94125ff19e2d85a462035ae11b723f84dec54a09ad21d217f97d3f05ce1bbc3f` for the pricing run. Publish a new immutable image.
- `register_phase5_pricing.ps1` and `run_phase5_pricing.ps1` now require `-ImageUri` with the newly verified immutable digest; they reject an unexpected registry/repository.
