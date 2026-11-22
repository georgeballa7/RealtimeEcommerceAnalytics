# Data model

Shopify events and historical source records keep their original identifiers and provenance. Verified crosswalks map Shopify customer/product/variant IDs to canonical historical identities; never join sources on SKU alone.

The Redshift dbt model uses `dim_date`, `dim_customers`, `dim_products`, `fct_orders`, `fct_order_lines`, `fct_cancellations`, and `fct_refund_lines`, plus daily sales and churn feature marts. **Cancellations are not settled refunds**. Preserve grain, `source_system`, `is_synthetic`, and event-time semantics.

See `analytics/dbt/` for schema definitions, entity relationships, tests and business rules.
