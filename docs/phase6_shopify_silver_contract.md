# Phase 6 — Live Shopify Bronze-to-Silver contract and Redshift staging reconciliation

Status: Glue processing, automated coordinator and Redshift loading are deployed; the latest daily run still requires separate verification.

## Processing boundary

Phase 5 Shopify -> SQS-backed webhook ingress -> S3 landing -> ECS projector -> confirmed RDS ingestion boundary
-> Glue PySpark JDBC extraction -> versioned S3 Bronze Parquet
-> Glue DQ / identity checks / quarantine -> S3 Silver JSONL.gz plus manifest
-> Redshift silver_staging (only committed Silver manifests) -> dbt staging/marts.

The original Pandas rules live in src/ecommerce/pipelines/silver/historical.py,
and the historical audit rules in src/ecommerce/pipelines/silver/audit.py.
This is a **policy port**, not an import of the Pandas implementation.

## Validated source-to-target column compatibility

The column ordering and major Redshift types below follow
analytics/redshift/silver_staging_schema.sql, which defines the Shopify landing tables in the live `silver_staging` schema.

| Spark Silver table | Redshift silver_staging target | Columns | Matching data types |
| --- | --- | ---: | --- |
| shopify_live_order | shopify_live_order | 13 | string/VARCHAR, long/BIGINT, timestamp/TIMESTAMPTZ, DECIMAL(18,2), boolean |
| shopify_live_order_line | shopify_live_order_line | 9 | string/VARCHAR, long/BIGINT, int/INT, DECIMAL(18,2), timestamp |
| shopify_live_refund | shopify_live_refund | 6 | string/VARCHAR, long/BIGINT, DECIMAL(18,2), timestamp |
| shopify_live_refund_line | shopify_live_refund_line | 6 | string/VARCHAR, long/BIGINT, int/INT, DECIMAL(18,2) |
| shopify_customer_identity | shopify_customer_identity | 7 | string/VARCHAR, timestamp |

The Silver stage must enforce VARCHAR limits before Redshift COPY, preserve
money as DECIMAL rather than floats, retain timestamps in UTC, and use
source-specific composite business keys.

## Reused historical policy concepts

- Immutable S3 Bronze evidence and a manifest written last.
- Reject null/invalid primary identifiers; never infer a missing customer.
- Stable customer/order/product identities, with Shopify ID provenance retained.
- Exact money types; validate negative amounts, order quantities, and refunds.
- Preserve adjustments/refunds separately from positive sales.
- Test primary key uniqueness, parent-child relations, and source-vs-accepted-
  plus-quarantined reconciliation before committing Silver.
- Never silently discard a rejected record.

**Historical-specific rule not reused**: Online Retail II two-worksheet
overlap deduplication does not apply to Shopify RDS tables.

**Source mapping is not automatically identity reconciliation**:
Historical synthetic orders have canonical IDs based on the cleaned historical
customer and stock-code maps; Shopify live records use Shopify IDs. The
verified Shopify-to-canonical customer/variant mappings must be checked against
the catalog and identity-sync evidence before unifying the star-schema facts.

## Operational restrictions

- Run Spark **only** after Phase 5 RDS projection has completed and application
  writes are quiescent; scheduled generation time alone is insufficient.
- Full JDBC table reads are not a PostgreSQL transaction-wide snapshot.
  Cross-table reconciliation is a necessary but insufficient consistency check
  if concurrent updates are possible. For strict transactional consistency,
  introduce an explicit source snapshot / coordinated CDC boundary before
  enabling concurrent production-scale extraction.
- Use one JDBC reader per table initially to avoid saturating the small RDS.
- Never run the job while RDS is stopping or restarting; verify the controller's Glue-aware lease spans the full extraction.
- Confirm source types against the actual PostgreSQL catalog and preserve a stable extraction boundary.
- The coordinator starts processing only after the committed slot marker and event-settlement checks.
- Terraform changes must preserve the existing generator, controller, and schedules.
- The old Python exporter is superseded for new runs; do not start its ECS task.
- Load only validated Silver manifests, with atomic reconciliation and idempotent warehouse operations.
- The `raw` → `silver_staging` schema rename is already complete; do not repeat it.

## Continuing acceptance checks

1. Validate the Spark script with local unit fixtures and AWS Glue syntax/permissions.
2. Confirm RDS source-column types against the actual PostgreSQL catalog.
3. Confirm source money precision, text lengths and accepted identity status values.
4. Confirm current Glue success and source/accepted/quarantine counts.
5. Verify the corresponding Redshift ledger and successful dbt build/tests.
6. Check idempotent slot markers, retry safety, and NAT resource release.
