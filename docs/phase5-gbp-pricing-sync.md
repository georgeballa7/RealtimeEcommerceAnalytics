# Phase 5 GBP catalog pricing

The pinned 3,408-SKU manifest is `config/catalog/phase5-gbp-sku-prices-v1.json`.
Source prices are **assumed** GBP based on the historical UK source, not currency-proven.
Shopify is authoritative for current catalog prices; historical Silver is immutable.

## Controlled sequence

1. Review migration 007 and run the existing versioned migration entrypoint using
   the **separate DDL role** inside the private VPC; do not run migrations on startup.
2. Build and deploy a reviewed worker image with the updated feature-branch code,
   existing Shopify OAuth environment variables and RDS TLS trust bundle.
3. Run `python scripts/sync_gbp_variant_prices.py --limit 10` for a **read-only**
   Shopify-vs-manifest preview (DB SELECT only). This requires VPC connectivity.
4. Review the preview and explicitly approve the initial write before using:
   `python scripts/sync_gbp_variant_prices.py --apply --limit 10 --confirm-sha256 7850c9f5faaea304b0d42c468a10179c8d44905f5ca82b7767e7497152fb2c06`
5. Verify Shopify read-back and RDS rows. The first release intentionally blocks
   write batches larger than 10; extending requires a separate reviewed change.

Shopify mutation errors and ambiguous network failures stop the run. Repeated runs
compare current Shopify prices first, so completed items are skipped. RDS is updated
only after an independent Shopify read confirms the actual current price. This is
not an atomic multi-system transaction, so a failed run must be reconciled before retry.

No S3 operational artifact or historical Silver mutation is performed by this runner.
