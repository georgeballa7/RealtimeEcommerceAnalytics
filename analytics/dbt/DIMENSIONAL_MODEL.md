# Phase 6 — dbt unified dimensional model (implementation and use cases)

## What dbt does here

dbt (data build tool) is a SQL transformation/test/documentation framework,
**not** a new language, an ingestion engine, a streaming framework, or an ML trainer.
Its templating is Jinja. `source()` declares Silver Redshift dependencies,
`ref()` declares transformations and yields an ordered lineage DAG.
Models are SQL SELECTs materialized as views or tables.

This model consumes **only Glue-validated S3 Silver already loaded to Redshift**
(not RDS operational tables or raw S3 objects). The 20 November 2026 Glue-to-Redshift export was independently reconciled. The dbt model itself is still only parsed offline; runtime materialization and all data tests remain pending.

## Redshift schema layout

- `silver_staging`: **physical source tables** copied from validated S3 Silver,
  including `synthetic_orders`, `synthetic_order_items`,
  `synthetic_cancellations`, the five Shopify operational projection tables,
  and `warehouse_load_ledger`. The physical `raw` → `silver_staging` rename completed on 20 November 2026.
- `analytics_staging`: dbt **views** `stg_synthetic_orders`,
  `stg_shopify_orders` (with profile schema `analytics` and default dbt schema
  naming from dbt_project.yml's `+schema: staging`).
- `analytics_marts`: dbt **tables**:
  3 dimensions, 4 facts, 1 BI aggregation, 2 churn feature outputs.
  With the current `profiles.example.yml`, dbt's default schema generator
  prefixes custom schemas with `analytics_`.

## Three conformed dimensions

| Dimension | Grain | Logical PK | Main attributes |
| --- | --- | --- | --- |
| dim_customers | one verified canonical customer | `customer_key` | first_order_at, latest_order_at |
| dim_products | one source-qualified product variant | `variant_key` | product_key (nullable until mapped), sku, source_system |
| dim_date | one calendar date, 2026–2040 | `date_key` | year_number, quarter_number, month_number, weekday_number |

For the current implementation, `dim_customers` includes all 5,942 verified customer identities from Silver plus any canonical historical order identities; sales timestamps are descriptive only and order measures remain in the facts.
`dim_products` derives historical and live variant keys directly from Silver source lines but does **not**
yet assert a cross-source product match. The RDS `shopify_product_identity`
and `shopify_variant_identity` ledgers hold verified crosswalks. They must be
added to Glue Silver and Redshift before mapping Shopify's `product_key` to
the canonical historical product UUID. Never infer equivalence from SKU alone.

## Four fact tables

| Fact | Grain / logical PK | Dimension FKs | Other event FKs | Measures |
| --- | --- | --- | --- | --- |
| fct_orders | one source-qualified order / `order_key` | `customer_key → dim_customers`; `order_date_key → dim_date` | none | gross_gbp, current_gbp |
| fct_order_lines | one source-qualified order line / `line_key` | `variant_key → dim_products`; `customer_key → dim_customers`; `order_date_key → dim_date` | `order_key → fct_orders` | quantity, unit_price_gbp, gross_line_gbp |
| fct_cancellations | one simulated cancellation / `cancellation_key` | `customer_key → dim_customers`; `variant_key → dim_products`; `cancellation_date_key → dim_date` | `order_key → fct_orders`; `line_key → fct_order_lines` | cancelled_quantity, cancelled_merchandise_gbp |
| fct_refund_lines | one recorded Shopify refund line / `refund_line_key` | `customer_key → dim_customers`; optional `variant_key → dim_products`; `refund_date_key → dim_date` | `order_key → fct_orders` | refund_quantity, recorded_refund_line_subtotal_gbp |

Redshift PK/FK declarations are informational, not reliably enforced.
The source RDS enforces PK/FK in OLTP; **dbt data tests** enforce logical
uniqueness, non-null business keys, referential integrity and allowed currency
in analytics. `source_system` and `is_synthetic` are retained on order facts.

### Business keys vs warehouse keys

- Synthetic source order: `synthetic_orders.order_id` (canonical source ID).
  Unified `order_key = synthetic_2026:<order_id>`.
- Shopify source order: `(shop_domain, shopify_order_id)`.
  Unified `order_key = shopify:<shop_domain>:<shopify_order_id>`.
- Synthetic order item: `order_item_id`; Shopify line:
  `(shop_domain, shopify_order_id, shopify_line_id)`.
- Shopify variant: `(shop_domain, shopify_variant_id)`, or an explicit
  source-scoped order-line placeholder if variant ID is absent.
  This is distinct from the historical canonical variant UUID until matched.
- Customer: verified `shopify_customer_identity.customer_id` refers to
  the pre-existing historical canonical UUID. The dbt staging model does
  not fall back to an unverified Shopify GID.

The key strings are deterministic **warehouse primary-key candidates** rather
than Redshift-enforced PK constraints. Their qualifiers prevent cross-source
ID collisions and allow repeatable upserts.

## Three analytical outputs

1. `mart_daily_sales`: Power BI orders, reported order gross/current totals,
   cancellation merchandise separately, date/source/simulation segmentation.
   A simulated cancellation is **not** a verified cash refund; never subtract
   it again from Shopify's latest-current order state to invent net revenue.
2. `mart_customer_churn_training`: customer × monthly as-of date, backward
   90-day frequency/spend/recency and **forward 30-day** label. The label is
   only calculated for complete observable horizons.
3. `mart_customer_churn_scoring`: latest unlabeled customer state used to
   generate MLflow predictions. Do not train on these current features.

**Churn limitation**: historical `current_gbp` is the *latest* order state;
using it in past as-of features leaks later refunds/status adjustments.
The training SQL needs immutable event-time amount snapshots (or documented
point-in-time derivation) before we can claim leakage-free churn evaluation.
Also use chronological customer-month holdouts, never randomly split rows.

## How dbt makes missing data visible

1. `dbt parse` verifies Jinja/SQL graph structure offline.
2. `dbt source freshness` requires timestamp configuration; not enabled
   until Silver commits and ingestion timestamps are defined for all sources.
3. `dbt run` materializes staging views and marts in topological order.
   Referencing a missing source column fails the model; no fabricated default.
4. `dbt test` checks unique/not_null keys, relationships, currencies, presence
   of the known historical baseline (>=14,110 orders, >=173,905 items,
   >=1,335 cancellations), Shopify composite keys and other data rules.
5. **Only after `dbt build` passes** should a stable snapshot become visible
   to Power BI and the churn training job. Tests are not redundant with
   the Glue per-snapshot reconciliation; dbt tests are warehouse-level.
6. No physical source row is assumed to exist just because a DDL definition
   or an empty table is present. The test named
   `assert_historical_backfill_present` fails on missing source rows.

## Dependency/operational ordering

Shopify ingested -> RDS projector completed -> Glue (RDS full snapshot →
S3 Bronze → validated S3 Silver) -> verified Silver manifest ->
transactional Redshift COPY/upsert to `silver_staging` ->
`dbt build` -> Power BI refresh / churn feature extraction ->
MLflow scoring and model-version tracking.

The source schema has been renamed and the validated Shopify Silver loader successfully loaded both 20 November snapshots. Before any dbt materialization, prepare the separate `analytics_staging` and `analytics_marts` namespaces for the dbt runtime identity and verify its SELECT access to Silver. These are not yet verified as deployed.
The pending dbt runtime requires a network-accessible private Redshift workgroup
and short-lived credentials; the example profiles.yml documents required
connection fields, not a deployed execution environment.

## Progressive modeling

Keep the 4 facts/3 dims as the minimum core. Next, add two verified product
identity ledger tables to the Glue Silver and Redshift contract; link Shopify
variant to historical canonical variant and product IDs. Then strengthen churn
with point-in-time snapshots, segmentation by source/synthetic status,
refund/return-frequency features and probabilistic MLflow predictions. Never
mix simulated cancellations with actual cash refunds.

## Reviewed star schema ERD

See [STAR_SCHEMA_ERD.md](STAR_SCHEMA_ERD.md) for the rendered Mermaid entity-relationship diagram, source lineage, seven-table core fact/dimension grains, and the target namespaces. Run `analytics/redshift/prepare_dbt_warehouse_schemas.sql` only with the verified dbt execution role; this step is not yet applied.

## Power BI model relationships

Use single-direction filters from `dim_date`, `dim_customers`, and
`dim_products` to the relevant fact tables. Avoid activating direct
fact-to-fact Power BI relationships simultaneously with all dimension paths:
those can introduce ambiguous filter propagation and double-counted measures.
Direct `order_key` / `line_key` references in the warehouse remain useful
for dbt integrity tests and drill-through logic. Do not sum order-level and
line-level gross amounts into the same KPI; they are different grains.
