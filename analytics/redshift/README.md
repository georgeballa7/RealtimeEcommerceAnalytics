# Phase 6 — Redshift / dbt / churn

**Status:** feature-branch implementation. No warehouse has been provisioned by the code commit itself.

## Data architecture

Immutable 2026 synthetic S3 backfill (manifest/CAS) -> Redshift `raw` -> dbt `staging` -> `marts`.
Operational Shopify webhook -> SQS -> S3 -> RDS -> **bounded RDS export to S3** -> Redshift `raw` -> dbt `staging` -> `marts`.
An RDS export is a consistent full snapshot, not a second streaming event consumer. Never mutate Bronze/Silver, and never interpret simulated cancellations as cash refunds.

The 2026 synthetic source currently covers 2026-01-01 through 2026-11-18T23:10:17Z, not the remainder of the calendar year. Current source counts: 14,110 orders, 173,905 lines, 1,335 simulated cancellations; pinned source is `historical/online_retail_ii/synthetic/v1/state/head.json`.

## Cost and deployment

- Optional Terraform resource `infrastructure/terraform/phase6_redshift.tf` defaults to `enable_redshift_warehouse=false`.
- 4-RPU base, max 8 RPU, daily 8 RPU-hour cutoff with `deactivate`; **not a monthly-dollar hard cap**. Storage, logs, transfers and dependencies may incur additional charges.
- Two application subnets in two AZs, enhanced VPC routing OFF, private workgroup; query with Data API. Reconcile actual account costs after first load.
- RDS remains independent and can stop between ingestion windows. An automated always-on RDS zero-ETL integration is intentionally not enabled because this demo uses a stop/start RDS controller.
- Terraform state must remain authoritative. Do not manually create a second workgroup outside its state.

Before provisioning: merge validated Phase 5 into main; run `terraform fmt -check`, `terraform validate`, and a reviewed `terraform plan -var enable_redshift_warehouse=true` using all existing pinned Phase 5 image variables (otherwise Terraform may propose destroying live resources). Apply the reviewed plan only when its diff is restricted to warehouse resources and explicitly check projected charges.
