# Keys, grains and lineage

## Operational PostgreSQL

Migrations 001–008 define identities, sync attempts and leases, catalog and price verification, webhook bookkeeping and operational projections. In this diagram repeated PK attributes form one composite key. Solid relationships represent declared foreign keys. The refund header's order ID is a reconciliation reference. Its corresponding order can arrive later.

```mermaid
erDiagram
    shopify_live_order ||--o{ shopify_live_order_line : contains
    shopify_live_refund ||--o{ shopify_live_refund_line : contains
    shopify_live_order {
        text shop_domain PK
        bigint shopify_order_id PK
        numeric current_total
    }
    shopify_live_order_line {
        text shop_domain PK, FK
        bigint shopify_order_id PK, FK
        bigint shopify_line_id PK
        int quantity
    }
    shopify_live_refund {
        text shop_domain PK
        bigint shopify_refund_id PK
        bigint shopify_order_id
    }
    shopify_live_refund_line {
        text shop_domain PK, FK
        bigint shopify_refund_id PK, FK
        bigint shopify_refund_line_id PK
        numeric subtotal
    }
    shopify_live_event_ledger {
        text shop_domain PK
        text webhook_id PK
        text s3_key
        char object_sha256
    }
```

The simulation run table records expected and completed order counts by run. Customer sync identity, attempts and leases share shop/customer identifiers with separate lifetimes. The catalog holds a product-to-variants FK and a price-state-to-variant FK. The webhook inbox/outbox migration defines a unique outbox reference to an inbox ID. Current ingress acknowledges SQS acceptance and can operate while RDS is stopped. See the [migrations](../../infrastructure/sql) and [identity foundation](../../README_IMPLEMENTATION.md).

### Identity and price constraints

```mermaid
erDiagram
    shopify_product_identity ||--o{ shopify_variant_identity : verified_product
    shopify_variant_identity ||--o| shopify_variant_price_state : latest_price
    shopify_webhook_inbox ||--o| shopify_webhook_outbox : unique_inbox_id
    shopify_product_identity {
        text shop_gid PK
        uuid product_id PK
        text shopify_product_gid
    }
    shopify_variant_identity {
        text shop_gid PK, FK
        uuid variant_id PK
        uuid product_id FK
        text shopify_product_gid FK
    }
    shopify_variant_price_state {
        text shop_gid PK, FK
        uuid variant_id PK, FK
        numeric price_gbp
    }
    shopify_webhook_inbox {
        bigint id PK
        text webhook_id
    }
    shopify_webhook_outbox {
        bigint id PK
        bigint inbox_id FK, UK
    }
```

## Redshift staging

The five live Shopify tables enter as one replaceable snapshot. Synthetic order, item and cancellation records come from the historical backfill. The manifest ledger identifies committed loads by key and digest. It provides batch lineage without a row-level foreign key to every business record. The relationships below describe business-key joins. Redshift correctness is checked by loader validation and dbt tests.

```mermaid
erDiagram
    synthetic_orders ||--o{ synthetic_order_items : order_id
    synthetic_orders ||--o{ synthetic_cancellations : order_id
    shopify_live_order ||--o{ shopify_live_order_line : shop_and_order
    shopify_live_refund ||--o{ shopify_live_refund_line : shop_and_refund
    shopify_customer_identity |o--o{ shopify_live_order : verified_gid
    synthetic_orders {
        varchar order_id
        varchar customer_id
    }
    shopify_live_order {
        varchar shop_domain
        bigint shopify_order_id
        varchar shopify_customer_gid
    }
    warehouse_load_ledger {
        varchar manifest_key
        varchar manifest_sha256
    }
```

The two staging views normalize historical and Shopify order rows. The Shopify view checks the verified customer mapping and filters GBP. The facts use source-qualified order and line keys. The analytical core comprises three dimensions and four facts in the marts schema. The branch defines no separate physical core schema. See the [landing DDL](../../analytics/redshift/silver_staging_schema.sql), [staging views](../../analytics/dbt/models/staging) and [analytical marts ERD](../../analytics/dbt/STAR_SCHEMA_ERD.md).

## Reading the marts

| Dataset | Grain | Meaning |
| --- | --- | --- |
| fct_orders | Source-qualified order | Count orders here. Gross and current GBP values describe the order state. |
| fct_order_lines | Source-qualified order line | Quantity and gross line value support product analysis. Joining back to orders repeats the header amount per line. |
| fct_cancellations | Simulated cancellation event | Cancelled merchandise is a synthetic outcome. |
| fct_refund_lines | Shopify refund line | Recorded line subtotal is evidence of a refund record. Cash settlement requires additional evidence. |
| mart_daily_sales | Order date, source system, synthetic flag | Aggregates gross/current order values and order count. Simulated cancellations join by cancellation date to synthetic rows. Dates without a matching order group do not appear through that join. |

## Customer-month features and parameter tables

Training examples require earlier orders and a fully observable next 30 days. The label is 1 when the customer places zero orders in that half-open future window. Predictors use prior orders for recency, 90-day order count and GBP spend, and lifetime order count. Current scoring uses the day after the latest observed order as its as-of date. Mutable current order totals can limit historical point-in-time fidelity.

```mermaid
erDiagram
    dim_customers ||--o{ mart_customer_churn_training : customer_key
    dim_customers ||--o| mart_customer_churn_scoring : customer_key
    churn_nb_priors_v1 ||--o{ churn_nb_likelihoods_v1 : label
    mart_customer_churn_training {
        varchar customer_key
        date as_of_date
        int churn_next_30d
        int recency_days
    }
    mart_customer_churn_scoring {
        varchar customer_key
        date as_of_date
        decimal spend_90d_gbp
    }
    churn_nb_priors_v1 {
        int label
        float prior_probability
    }
    churn_nb_likelihoods_v1 {
        int label
        varchar feature
        varchar value
        float conditional_probability
    }
```

These are logical grains. Training is unique by customer and as-of date. Current scoring yields one row per eligible customer. The likelihood table has one row per class, feature and bin. The prediction view evaluates both candidate classes and emits customer, as-of date, cohort, probability and predicted label. Validation rows have observed labels. Current rows have none. The September–October temporal holdout recorded ROC-AUC 0.6398. Calibration and provenance limit how the scores can be interpreted.

[Fact and mart SQL](../../analytics/dbt/models/marts) · [Model fit and scoring SQL](../../scripts/redshift/train_churn_naive_bayes.sql) · [Evaluation and limitations](../churn_sql_model_v1.md)
