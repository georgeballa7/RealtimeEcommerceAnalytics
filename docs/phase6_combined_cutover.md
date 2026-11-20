# Phase 6 coordinated Shopify simulation → Glue → Redshift release

Status: code staged on `feature/redshift-warehouse-dbt-churn`; **not deployed by this document**. All clock times Europe/Berlin. AWS eu-central-1, account 825414820081. Never use a manual Terraform apply without restoring the pinned Phase 5 image variables from deployed state and reviewing the plan.

## Live AWS inventory verified 20 November 2026

| Resource | Observed live state |
| --- | --- |
| Shopify generation 08:00 | EventBridge `ecommerce-demo-phase5-morning` ENABLED, ECS generator `:5` |
| Shopify generation 16:00 | EventBridge `ecommerce-demo-phase5-afternoon` ENABLED, ECS generator `:5` |
| Legacy lifecycle 18:00 | `ecommerce-demo-phase5-lifecycle` ENABLED, ECS lifecycle `:2`; do not remove prematurely |
| Glue readiness poll | `ecommerce-phase6-evening-glue-demo` ENABLED, `cron(0/5 16-21 * * ? *)` Berlin (morning coverage not yet deployed) |
| Glue PySpark job | `ecommerce-phase6-shopify-silver-demo` deployed; 2 successful manual/afternoon runs documented |
| Redshift Silver loader | `ecommerce-phase6-silver-redshift-loader-demo` deployed ZIP; new completion acknowledgment not yet deployed |
| Redshift Serverless | `ecommerce-analytics-demo` AVAILABLE, base 4 RPU; daily 8 RPU-hour logging and monthly 16 RPU-hour deactivation |
| RDS | `ecommerce-postgres-dev` db.t4g.micro; observed stopped |
| NAT | one **static** Terraform-owned gateway; both private application subnets use its default route |
| Controller | DynamoDB `ecommerce-demo-phase5-controller` has `phase6-evening` batch `20261120T161030Z` with Glue SUCCEEDED / `dispatched` (not terminal warehouse acknowledgment) |

Source of truth for the *next* deployment is a fresh live AWS inventory and the reviewed Terraform plan, not this snapshot.

## Required readiness contracts

1. Existing ECS generator schedule starts one combined task: deterministically generate approved synthetic Shopify orders, then evaluate eligible cancellations and transactionless synthetic refunds from the journal. A successful task writes `simulation/phase5/slots/v1/live-YYYYMMDD-{morning|afternoon}.json` **last**.
2. All synthetic refunds are distinct from cancellation. Policy `config/phase5-synthetic-refund-policy.json` selects 5% of non-cancellation orders, one item quantity, delayed 0–2 days; only orders with no Shopify payment transactions and `PENDING`/`VOIDED` financial state may receive automated `refundCreate` with empty transactions and no restock. No real-money refund may be automatically initiated. An ineligible order is skipped, not falsely reported as refunded. Shopify's live API may reject transactionless refunds; do not claim success without a development-store smoke.
3. Coordinator refuses Glue until the *matching* S3 combined-slot marker exists and the webhook event index, SQS, ECS tasks and projector are quiescent. A marker is necessary but not by itself proof every possible delayed webhook arrived: verify expected event identities and reconciliation during release.
4. Glue extracts full RDS table state through JDBC, validates Silver and commits immutable manifest last. Current per-table JDBC reads are not a transaction-wide snapshot. Do not allow concurrent projector writes during extraction.
5. Redshift loader must reconcile manifest, commit staging plus ledger, and acknowledge matching DynamoDB slot `completed`. An asynchronous Lambda 202 is only `dispatched`, not completed.
6. RDS controller Glue lease must protect database startup/stop over Glue execution. Coordinator's own lease has a 45-minute TTL; review recovery path for abandoned Glue runs and no premature stop. NAT removal is allowed only once all network-dependent work is idle.
7. Never classify simulated cancellations or Shopify refund line subtotals as settled cash reimbursements.

## Guarded staged cutover, in this order

- [ ] **Preflight:** Read AWS schedules/target ECS revisions, Lambda SHA/roles, RDS/Glue/NAT/Redshift states and DynamoDB; ensure current daily runs are idle and write no pending; review Shopify webhook subscription including `REFUNDS_CREATE`, verify mock shop and API permissions.
- [ ] **Verify CI:** Python CI, Glue PySpark, Terraform validate, dbt offline, and PowerShell syntax pass on **the exact current commit**. Review changes and Terraform plan with `enable_phase5_legacy_lifecycle_schedule=true`, `nat_gateway_mode=static`; do not inadvertently destroy existing Redshift, RDS, ECR, ECS, schedules, S3 or IAM.
- [ ] **Stage IAM & images:** Apply only reviewed IAM/S3 access for combined ECS generator lifecycle journal + marker, existing Glue coordinator marker read, and Redshift loader DynamoDB acknowledgments. Build/push fresh digest-pinned generator container (including lifecycle and policy), update scheduled ECS task definition atomically through reviewed Terraform. Phase5 controller Glue-aware image/lease must be deployed and verified as part of the existing guarded `scripts/aws/deploy_phase6_rds_controller.ps1` release if not already present. Do not change 18:00 yet.
- [ ] **Deploy loader ZIP:** Validate `scripts/aws/deploy_phase6_silver_loader.ps1` with no `-Apply`, review IAM simulation, then use `-Apply`. Verify code hash and handler. No automatic replay until coordinator is ready.
- [ ] **Deploy coordinator ZIP:** Validate then apply `scripts/aws/deploy_phase6_evening_coordinator.ps1`. Confirm readiness schedule becomes `cron(0/5 8-21 * * ? *)` Berlin (not another generator schedule); verify Lambda zip hash and S3 marker permissions.
- [ ] **Morning/afternoon acceptance:** Observe an **entire** morning and afternoon combined slot: exactly requested order count, completed task, cancellation/refund journal outcomes, S3 slot markers, successful webhook projector, no pending indexed events or SQS backlog, Glue SUCCEEDED and manifest rows reconciled, Redshift transactional load ledger with matching manifest, coordinator DynamoDB `phase=completed`. A refund-selection outcome is not a Shopify-accepted refund; verify actual Shopify `refunds/create` delivery, RDS `shopify_live_refund`/`refund_line`, S3 Silver and Redshift rows. Test one harmless eligible refund in a controlled preview/smoke; if no eligible transactionless Shopify order exists, keep auto refund disabled until safe simulation approach is verified.
- [ ] **Failure testing:** Simulate missing marker, generator failure, unsettled cancellation, deferred/replayed/refused refund, missed webhook, Glue failure and loader timeout. Verify no double creation/refund and no false `completed` state; inspect expired DynamoDB leases and use manual reconciliation rather than unsafe clearing.
- [ ] **Retire 18:00:** After both daily workflows have passed, reviewed Terraform apply `enable_phase5_legacy_lifecycle_schedule=false`; verify actual EventBridge resource removed and no independent cancellation batch. Preserve ECS task definition/image until rollback window expires.
- [ ] **On-demand NAT optional separate cutover:** Review `nat_gateway_mode=on_demand` Terraform plan, EIP/route ownership and 07:30/15:30 prewarm. Confirm S3 endpoint access and ECS ECR/CloudWatch/secrets readiness, ensure no 18:00 need, then migrate only during an idle window. The NAT Lambda must never delete Terraform-owned static gateway directly. Validate routing prewarm and deletion safety on both windows and monitor spending.
- [ ] **dbt/ML readiness separately:** prepare analytics staging/marts schemas and IAM, publish pinned dbt runner image, run `dbt build` / full data tests; verify source identities and point-in-time churn features before MLflow training/scoring. Do not claim Gold/MLflow production ready from offline parser validation alone.

## Operator safeguards and rollback

* Current schedule and Lambda versions stay unchanged until a reviewed deployment actually executes. A merged branch does **not** update ECS/Glue/Lambda by itself.
* New S3 marker is backwards-incompatible with old producer. Update producer IAM/image **before** deploying coordinator; keep old 18:00 schedule until verified end-to-end run.
* Rollback to old coordinator only after checking in-flight Glue and loader statuses. Retain 18:00 while testing. Rolling back the producer without rolling back marker-required coordinator would halt the analytics pipeline.
* Restore Terraform-controlled static NAT only via reviewed Terraform plan after all dynamic NAT routes and owned gateways are reconciled. Never destroy a NAT gateway while a Shopify/ECS workload depends on it.
* The daily 8 RPU-hour threshold logs; only monthly 16 RPU-hours deactivates compute. Neither represents a total AWS budget cap.
* Avoid repeated Shopify mutations to probe readiness. Preview/dry-run first; exact journals and manifest IDs make reruns idempotent, but an ambiguous Shopify refund must fail closed and be manually reconciled.

## Core review files

`scripts/phase5_slot_runner.py`; `scripts/run_phase5_lifecycle.py`; `src/ecommerce/application/use_cases/shopify_order_lifecycle.py`; `scripts/aws/phase6_evening_coordinator.py`; `scripts/redshift/load_shopify_silver.py`; `scripts/aws/phase6_nat_lifecycle_lambda.py`; `infrastructure/terraform/phase5_generator.tf`; `infrastructure/terraform/phase5_scheduled_lifecycle.tf`; `infrastructure/terraform/phase6_glue_silver.tf`; `infrastructure/terraform/phase6_nat_on_demand.tf`; `scripts/aws/deploy_phase6_silver_loader.ps1`; `tests/unit/test_phase6_synthetic_refund_policy.py`.
