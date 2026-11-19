# Phase 5 GBP catalog pricing

The pinned 3,408-SKU manifest is `config/catalog/phase5-gbp-sku-prices-v1.json`.
Source prices are **assumed** GBP based on the historical UK source, not currency-proven.
Shopify is authoritative for current catalog prices; historical Silver is immutable.

## Controlled sequence

1. Review migration 007 and run the existing versioned migration entrypoint using
   the **separate DDL role** inside the private VPC; do not run migrations on startup.
2. Build and deploy a reviewed worker image with the updated feature-branch code,
   existing Shopify OAuth environment variables and RDS TLS trust bundle.
3. Run `python scripts/sync_gbp_variant_prices.py --limit 3408` for a **read-only**
   Shopify-vs-manifest preview (DB SELECT only). This requires VPC connectivity.
4. Review the full-catalog preview, then explicitly approve one complete run:
   `python scripts/sync_gbp_variant_prices.py --apply --limit 3408 --confirm-sha256 7850c9f5faaea304b0d42c468a10179c8d44905f5ca82b7767e7497152fb2c06`
5. Verify Shopify read-back and all 3,408 RDS rows. The runner checkpoints
   each successfully verified variant in RDS and logs aggregate progress every 100 items.

Shopify mutation errors and ambiguous network failures stop the run. Repeated runs
compare current Shopify prices first, so completed items are skipped. RDS is updated
only after an independent Shopify read confirms the actual current price. This is
not an atomic multi-system transaction, so a failed run must be reconciled before retry.

No S3 operational artifact or historical Silver mutation is performed by this runner.

## Deployment with the existing Docker credential-helper workaround

Use `scripts/aws/publish_ecr_image.ps1 -Workload phase5_pricing` to build/push
without Docker login (its temporary `DOCKER_CONFIG` contains ECR credentials
only during the push). Publish an immutable digest and enable the on-demand
task definition through `phase5_pricing_image` after reviewing Terraform plan.

For a preflight ECS task, use the default command `["--limit","3408"]`.
For the authorized **single full-catalog run**, override command to:
`["--apply","--limit","3408","--confirm-sha256","7850c9f5faaea304b0d42c468a10179c8d44905f5ca82b7767e7497152fb2c06"]`.

Do not launch this task until RDS migration 007 and the app-role grants have
been verified. A restart performs independent Shopify reads and will skip
already-matching writes. All 3,408 SKUs are processed in one ECS execution;
each successful price is committed separately to RDS.
