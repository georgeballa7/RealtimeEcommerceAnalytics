# Phase 6 Glue-aware RDS lease — staged rollout (not deployed)

Status: **feature branch implementation only**. The currently deployed Phase 5
Lambda image does **not** yet inspect the Glue processing lease. Do not use
`-UseGlueLease` until controller-image and IAM deployment is verified.

## Existing topology and safety

- Lambda `ecommerce-demo-phase5-controller` runs every minute; its DynamoDB
  record in `ecommerce-demo-phase5-controller` already has a **50-second
  controller mutex** (`lease_token`, `lease_until`).
- RDS idle stop is enabled with a 30-minute quiet period.
- Glue 5.0 PySpark (`ecommerce-phase6-shopify-silver-demo`) uses 2 G.1X
  workers, one concurrent run, and a 20-minute job timeout.
- Reuse the existing DynamoDB record and Lambda. No Step Functions,
  additional tables, schedules, or always-running infrastructure needed.

## Lease contract

New attributes on the **same** controller item:
`glue_lease_owner`, `glue_lease_expires_at` (Unix seconds),
`glue_lease_job_run_id` (AWS Glue `jr_...` ID).

- `reserve`: conditional DynamoDB UpdateItem, requiring controller mutex
  absent/expired **and** no previous Glue owner. The controller's final
  stop decision runs while holding that same mutex. Prevents an
  acquire-versus-stop race.
- `attach`: owner-conditional and only while the lease is unexpired; run
  ID can be attached once. Glue may start before the ID is persisted, so an
  orphaned reservation without an ID is intentionally a manual-recovery case.
- `renew`: owner-conditional, only if unexpired. Launcher renews each 30s
  during Glue polling.
- `release`: owner-conditional. Helper first reads a strongly consistent
  record and confirms the recorded Glue job is terminal using GetJobRun.
- `controller`: protects RDS while the lease is unexpired. On expiry
  with a tracked job, queries Glue and keeps RDS available if running or if
  status cannot be verified. On expiry without a tracked run ID, fails
  closed until operator reconciliation.

No automatic retries, no automatic reruns, and no unsafe reclaims. The
historical/Shopify source data is not changed.

## Safe phased rollout

1. Commit/test controller code, helper, launch opt-in and IAM Terraform.
   Default `enable_phase6_glue_rds_lease=false` produces no change to
   deployed Phase 5 Lambda.
2. Build/push a **new immutable** Phase 5 Lambda ECR image digest from the
   current feature branch. Do not reuse the original image tag.
3. Prepare a guarded Terraform plan using deployed settings and the new
   `phase5_lambda_image`, with `enable_phase6_glue_silver=true`,
   `enable_redshift_warehouse=true` and
   `enable_phase6_glue_rds_lease=true`. **Allow only** the intended Lambda
   image/environment update and the new controller Glue-read IAM policy.
   Review carefully: the Phase 5 Lambda image also backs the FastAPI and
   landing handlers, so updating `phase5_lambda_image` can update multiple
   Lambda resources. Confirm these are deliberate and regression tested.
4. Apply only after explicit approval; verify controller environment variable
   `PHASE6_GLUE_JOB_NAME`, configured Glue IAM permission, deployment image
   digest and controller CloudWatch health.
5. Only then use
   `./scripts/aws/launch_phase6_glue.ps1 -Launch -UseGlueLease`.
   The script checks the deployed controller's configured job name and
   refuses protected mode otherwise.
6. Observe successful lease reservation, RDS startup, Glue execution,
   Silver manifest, terminal release, and normal eventual idle shutdown.
7. Test controlled failure/abandon and operator recovery. Do **not**
   prematurely clear an orphan lease merely because its timestamp expired:
   inspect Glue run history for a possible in-flight run.

## Rollback

Do not change the running controller back to an image that ignores leases
while a leased Glue job is active. Stop enabling new Glue runs, verify
tracked job terminal, release the lease, then review a guarded Terraform
rollback of Phase 5 image and opt-in IAM/env. Keep Phase 5 ingestion alive.

## Known next challenges

- An RDS lease protects availability but does **not** create a consistent
  cross-table PostgreSQL snapshot. Snapshot fencing / a stable projector
  checkpoint remains separate work.
- The first verified Glue Silver batch is
  `warehouse/shopify_snapshots/v1/20261120T112657Z/silver_manifest.json`;
  source and Silver counts matched (5,942 customers, 46 orders, 95 lines,
  0 refunds/lines, 0 quarantined).
- Redshift schema migration remains separate; no Glue-aware controller
  deployment or Redshift mutation was performed while implementing this.
