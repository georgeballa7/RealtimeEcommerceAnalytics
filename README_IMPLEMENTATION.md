# Shopify Customer Identity Synchronization — Implementation Status

**Committed foundation:** [26467dd](https://github.com/georgeballa7/RealtimeEcommerceAnalytics/commit/26467ddcfb86dbbdd87f672eab5353033a8b6f55) on `feature/historical-data-foundation`. This includes the Shopify GraphQL client, customer identity SQL, PostgreSQL mapping repository, guarded small-batch sync use case and seven offline unit tests. This document records the implementation state; it is not an uncommitted proposal.

**Verified externally on 17 November 2026:** The installed Shopify development-store app authenticated through the Admin GraphQL API; shop identity, the CUSTOMER metafield definition of type `id` at `custom.ecommerce_canonical_customer_id`, and granted `read_customers` / `write_customers` scopes passed a read-only Python check. No Shopify customer creation was performed during that verification.

**Not yet executed or verified:** The SQL migration has not been verified as applied to RDS. An AWS customer-sync ECS/Fargate task is not deployed. No live customer write, complete 5,942-customer reconciliation or bulk import has been performed. The existing SQL has uniqueness safeguards but no durable per-attempt ledger; the existing script is limited to five writes per invocation.

## AWS-only deployment plan

AWS `eu-central-1` is the sole application runtime. Shopify is the external development-store source. Private RDS PostgreSQL stores verified customer mappings and future durable sync state; S3 holds immutable historical Silver v1. The worker will run as an on-demand ECS/Fargate task in the existing VPC, using least-privilege IAM, Secrets Manager, verified RDS TLS and CloudWatch logs. Developer machines may run unit tests and CLI tooling, but are not a second operational deployment.

1. Add versioned, transaction-safe RDS migration execution, create a least-privilege application role and configure AWS credential injection; deploy only after review.
2. Add a durable customer-attempt ledger, bounded backoff, recovery and reconciliation for ambiguous Shopify outcomes, and a resumable bulk runner with progress and final-count reporting.
3. Package and deploy the customer-sync task using the existing private network; validate configuration, health/logging and failure handling without making the database public.
4. Execute a read-only synchronization verification against checksum-verified Silver v1 before authorizing Shopify writes.
5. Explicitly approve one Shopify customer create/find/retry integration test; verify whether `customerSet(identifier: ..., input: {})` is accepted for an ID-only historical customer. Never invent email addresses or consent.
6. Only after the integration tests pass, run bounded synchronization for all 5,942 canonical Silver customers and reconcile persisted Shopify-returned GIDs with zero mapping conflicts.

## Existing developer checks

From the repository root:

```powershell
python -m pip install -e ".[shopify,postgres,historical,aws,dev]"
python -m pytest tests/unit/test_shopify_customer_sync.py -q
```

The current `scripts/sync_historical_customers.py` entrypoint is read-only by default and permits at most five writes when explicitly enabled. It expects `SHOPIFY_SHOP_DOMAIN`, `SHOPIFY_ADMIN_TOKEN` and `ECOMMERCE_DATABASE_URL`, and a reachable PostgreSQL database. Do not run it as a local-to-private-RDS connection workflow; it needs AWS task packaging and a deployed schema first. Never store or publish credentials in GitHub or logs.

**Safety boundary:** PostgreSQL transactions cannot atomically commit a remote Shopify operation. Durable attempt tracking plus read-back reconciliation is required to make retries safe across that boundary. Silver v1 remains immutable.
