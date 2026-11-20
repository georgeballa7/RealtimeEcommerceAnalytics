# Phase 6 — Shopify development-store 22:00 lifecycle and Redshift cutover

Status (20 November 2026): **schedule changed in live AWS; updated ECS/Lambda code remains on feature branch until a reviewed release**. Branch: `feature/redshift-warehouse-dbt-churn`. Region: `eu-central-1`. Timezone: `Europe/Berlin`.

## Confirmed daily business schedule

- 08:00: Shopify development-store order generator (100 orders).
- 16:00: Shopify development-store order generator (150 orders).
- 22:00: standalone cancellation and simulated refund lifecycle (including previously created orders due today).
- After 22:00: webhook delivery → SQS → RDS projector; only after completion and a quiet window → Glue PySpark JDBC → S3 Bronze/validated Silver → manifest → Redshift Silver staging and load ledger.
- Morning Glue snapshot remains optional for early order analytics; **the nightly snapshot is the authoritative end-of-day lifecycle-aware state**. Morning snapshots must not be presented as final daily refund-inclusive revenue.

AWS Scheduler `ecommerce-demo-phase5-lifecycle` was verified updated directly from `cron(0 18 * * ? *)` to `cron(0 22 * * ? *)`, while its existing target and ENABLED state were preserved. The Terraform schedule is now aligned to 22:00. **The new lifecycle image has not been released to ECS yet**; the existing image still handles the deployed schedule until release.

## Commit contracts

1. **Generation:** `scripts/phase5_slot_runner.py` writes `simulation/phase5/slots/v1/live-YYYYMMDD-{morning|afternoon}.json` only after a successful applied run. No refunds or cancellations execute in the generator.
2. **Nightly:** `scripts/run_phase5_lifecycle.py --apply --confirm-apply --require-settled --commit-nightly --limit 500` scans eligible generation journals (16-day lookback), deterministically selects 10% cancellations plus 5% refund candidates (0–2-day delay), operates on Shopify and waits for asynchronous cancellations to settle. Then verifies both today's generation markers and writes `simulation/phase5/slots/v1/lifecycle-YYYY-MM-DD.json`. Failure or unsettled actions must leave marker absent.
3. **Refund policy:** `config/phase5-synthetic-refund-policy.json`, version `phase5-synthetic-refund-policy-v1`, explicitly targets the configured development store. Eligible candidates are unpaid/voided transactionless orders, or orders where **every listed payment transaction is marked `test=true`** by Shopify. Any non-test/unknown payment transaction is excluded. One refundable line and one unit maximum per selected order. The `refundCreate` request uses stable Shopify `@idempotent` key, `transactions: []`, `NO_RESTOCK`, and no customer notification. Shopify may reject an ineligible order; log and reconcile; never present a simulated refund as verified cash settlement.
4. **Webhook projection:** Shopify `REFUNDS_CREATE` is already part of registered topics; projection persists `shopify_live_refund` and `shopify_live_refund_line` in RDS under its idempotent event ledger, after SQS landing. Shopify order update/cancellation webhook records remain independent from refund line records.
5. **Glue barrier:** revised `phase6_evening_coordinator.py` polls at `cron(0/5 0-2,8-15,22-23 * * ? *)` Berlin. The night slot checks today's 08:00 + 16:00 order-generation markers and 22:00 lifecycle marker, then checks RDS controller readiness and SQS/worker quietness. 00:00–02:59 Berlin belongs to **previous day's nightly slot**. Night Glue must not start before the lifecycle marker exists. A quiet queue by itself is not a run completion certificate.
6. **Glue/Redshift:** Glue checks row-level quality and source/Silver reconciliation and commits its Silver manifest last. Redshift must load a matching committed manifest, verify row counts, transactionally update its Silver staging and load ledger, and acknowledge `phase=completed` in DynamoDB. Do not confuse coordinator `dispatched` with Redshift `completed`. dbt `fct_refund_lines` models line-item subtotals with financial_semantics `not_payment_settlement`, separate from cancellations.
7. **NAT:** if on-demand NAT is deployed in a *separate reviewed cutover*, the 16:00 window must remain network-accessible for 22:00 Shopify operations and post-midnight projection. This costs more NAT runtime than two short windows. Implement a split 16:00/22:00 prewarm if cost optimization is prioritized; **the on-demand NAT code should not be enabled without its midnight lease/route regression review**.

## Verified AWS state before image rollout

- ECS generator revision `:5`, lifecycle revision `:2` (both existing images), morning/afternoon schedules ENABLED.
- 22:00 lifecycle schedule ENABLED and target preserved; no current AWS ECS image contains this feature branch's newest refund policy until new digest is built and applied.
- Glue job `ecommerce-phase6-shopify-silver-demo` deployed; two SUCCEEDED jobs observed on November 20.
- Redshift Serverless `ecommerce-analytics-demo` AVAILABLE; daily 8 RPU-hour log and monthly 16 RPU-hour deactivate limits.
- RDS `ecommerce-postgres-dev` observed stopped; static Terraform-managed NAT gateway observed available.
- Phase 6 readiness Lambda still deployed at earlier 16–21 poll; deploy revised ZIP *after* generator/lifecycle images are correctly scheduled, otherwise marker gating blocks all old tasks.
- Audit query identity received `permission denied for schema silver_staging`; inspect role grants separately, not by relaxing the source-to-Silver checks.

## Reviewed deployment sequence

- [ ] Confirm all 5 CI workflows pass on the **exact final branch SHA**, including nightly marker, temporal rollover, and Shopify refund policy regressions.
- [ ] On the Windows checkout, confirm current branch and clean worktree. Run the reviewed `scripts/aws/deploy_phase6_rds_controller.ps1` in default plan-only mode, with AWS profile `ecommerce-admin`. It builds/pushes immutable Lambda, generator and lifecycle images, pins existing projector and other variables, checks actual 22:00 schedule, and previews Terraform. Inspect all IAM/ECS/Scheduler changes; no deletion of RDS/Redshift/VPC resources or NAT cutover.
- [ ] After reviewed Terraform plan, run `scripts/aws/deploy_phase6_rds_controller.ps1 -Apply` with exact approval string; verify new lifecycle and generator digests in EventBridge schedule targets and controller Glue/RDS lease support. Confirm no other schedules changed.
- [ ] Deploy updated Redshift loader after its IAM preflight using `scripts/aws/deploy_phase6_silver_loader.ps1`; validate dry-run then `-Apply`. Check version SHA and DynamoDB write acknowledgment.
- [ ] Deploy updated readiness coordinator via `scripts/aws/deploy_phase6_evening_coordinator.ps1`; first validate ZIP, then verify new `0-2,8-15,22-23` cadence, IAM S3 marker read and DynamoDB transitions.
- [ ] Verify 22:00 Shopify lifecycle in actual dev shop: selected test-gateway refunds (and non-test exclusion), operation journal idempotency, `refunds/create` and `orders/updated`, RDS refund rows and refund-line subtotals, S3 Bronze/validated Silver, Redshift load ledger and `fct_refund_lines`. Prove delayed Shopify webhooks are processed or deferred to the next batch and never silently lost.
- [ ] Manually test failure paths (missing marker, Shopify mutation error, asynchronous cancellation unresolved, RDS unavailable, Glue failure, delayed webhooks, Redshift loader 202 but no acknowledgment, night across midnight).
- [ ] Validate dbt build and warehouse access grants; actual churn training and MLflow are separate release gates.
- [ ] Migrate static NAT to on-demand only with an explicit idle-window Terraform plan, then verify 16:00 and 22:00 network continuity and costs.

## Rollback

Terraform state remains authoritative for images/schedules, except the already performed live EventBridge 22:00 timing change which is reflected in branch Terraform. The older ECS images remain available for digest rollback. Do not revert coordinator while an active Glue lease/loader is running, and do not invoke an unreviewed old producer after deploying marker-gated coordinator. If the 22:00 Shopify lifecycle fails, mark night uncommitted and preserve source/webhook evidence for safe reconciliation rather than publishing a misleading Redshift snapshot.

This document supersedes the previous 18:00 retirement and twice-daily combined-generation proposal; those earlier assumptions no longer reflect the user's requested operating schedule.
