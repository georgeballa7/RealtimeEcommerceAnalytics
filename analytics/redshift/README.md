# Phase 6 — Redshift / dbt / churn

**Status:** feature-branch implementation. No warehouse has been provisioned by the code commit itself.

## Data architecture (revised Phase 6)

Verified historical 2026 synthetic backfill (S3 Silver-derived) -> existing
Redshift schema **silver_staging** (currently still named **raw** in AWS pending
reviewed in-place rename) -> dbt staging and marts.

Shopify webhooks -> SQS -> RDS projector -> **successful ingestion barrier**
-> on-demand AWS Glue **PySpark JDBC read** -> immutable S3 Bronze Parquet
-> Glue cleaning/reconciliation -> S3 Silver JSONL.gz and quarantine + manifest
-> Redshift **silver_staging** -> dbt staging and marts.

The old direct Python RDS exporter is superseded, not the production path.
Its ECS resources are not yet removed because cleanup requires a separate
reviewed Terraform plan. There is **no live Shopify export or warehouse load yet**.

Glue is Terraform opt-in and is NOT deployed or scheduled by this commit. The
controller still needs a job-duration RDS lease, completion event trigger and
warehouse load integration before the twice-daily ingestion-driven pipeline
can be enabled. See `docs/phase6_shopify_silver_contract.md`.

The 2026 synthetic source currently covers 2026-01-01 through
2026-11-18T23:10:17Z, not the remainder of the calendar year. Current source
counts: 14,110 orders, 173,905 lines, 1,335 simulated cancellations; pinned
source is `historical/online_retail_ii/synthetic/v1/state/head.json`.

## Cost and deployment

- Optional Terraform resource `infrastructure/terraform/phase6_redshift.tf` defaults to `enable_redshift_warehouse=false`.
- 4-RPU base, max 8 RPU, daily 8 RPU-hour cutoff plus a **monthly 16 RPU-hour** cutoff with `deactivate`; around 4 hours of base-capacity compute monthly if it never scales. Neither threshold is a dollar cap or a guarantee against overage, in-flight billing, storage or ancillary charges. Based on a Frankfurt reference rate of $0.451 per RPU-hour, 16 RPU-hours corresponds to ~$7.22 of warehouse compute (subject to AWS billing rates and change).
- Two application subnets in two AZs, enhanced VPC routing OFF, private workgroup; query with Data API. Reconcile actual account costs after first load.
- RDS remains independent and can stop between ingestion windows. An automated always-on RDS zero-ETL integration is intentionally not enabled because this demo uses a stop/start RDS controller.
- Terraform state must remain authoritative. Do not manually create a second workgroup outside its state.

Before provisioning: merge validated Phase 5 into main; run `terraform fmt -check`, `terraform validate`, and a reviewed `terraform plan -var enable_redshift_warehouse=true` using all existing pinned Phase 5 image variables (otherwise Terraform may propose destroying live resources). Apply the reviewed plan only when its diff is restricted to warehouse resources and explicitly check projected charges.
