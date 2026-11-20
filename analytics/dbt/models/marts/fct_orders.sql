{{ config(sort=['order_timestamp_utc']) }}
-- One row per business order. order_key is source-qualified to avoid collisions.
-- Customer_key is the verified canonical customer identity, not a Shopify GID.
with unified as (
    select * from {{ ref('stg_synthetic_orders') }}
    union all
    select * from {{ ref('stg_shopify_orders') }}
)
select
    order_key, source_order_id, customer_key,
    order_timestamp_utc,
    cast(order_timestamp_utc as date) as order_date_key,
    gross_gbp, current_gbp, cancelled_at, financial_status,
    source_system, is_synthetic, currency, batch_id
from unified
