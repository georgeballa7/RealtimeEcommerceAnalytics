# Shopify Customer Sync — Review Package

**Scope:** review proposal only. No GitHub commit, database migration, Shopify write, or S3 change has been performed.

Files are relative to repository root. Install `python -m pip install -e '.[shopify,postgres,historical,aws,dev]'` then run `python -m pytest tests/unit/test_shopify_customer_sync.py -q`.

Before any live writes:
1. Install/configure the Shopify app with `read_customers` and `write_customers` scopes and protected customer data access as applicable.
2. Create a Shopify CUSTOMER metafield definition of type `id`: namespace `custom`, key `ecommerce_canonical_customer_id` (configured once, not via this script).
3. Apply `infrastructure/sql/001_shopify_customer_identity.sql` with an authorized migration role to the operational database. Use a least-privilege application role with TLS verification.
4. Export environment variables `SHOPIFY_SHOP_DOMAIN`, `SHOPIFY_ADMIN_TOKEN`, `ECOMMERCE_DATABASE_URL`; never paste them into the chat or Git.
5. First run without `--allow-customer-writes`: the script reads checksum-verified Silver from S3, verifies shop and metafield definition, and only does remote custom-ID lookups. It writes no customer records.
6. After reviewing read-only output, run a controlled test with `--allow-customer-writes --limit 1` (max 5 per invocation); investigate success and retry behavior before broader rollout.
7. Full population reconciliation (5,942 IDs) requires a separately reviewed bulk runner with progress reporting; this package intentionally cannot bulk-create customers.

**Important:** `customerSet` receives `input: {}` because historical UCI data does not contain genuine customer names/emails. This is a documented API schema shape, but its acceptance for customer creation must be verified against the development store on a single explicitly approved test identity. Do **not** invent email addresses or imply any user consent.

The submitted SQL defines mapping uniqueness. It does not yet contain a durable per-attempt retry ledger, which is a subsequent operational hardening step. A failure between Shopify creation and the DB insert is recovered by lookup-by-custom-ID on restart.
