{{ config(sort=['order_timestamp_utc']) }}
select * from {{ ref('stg_synthetic_orders') }}
union all
select * from {{ ref('stg_shopify_orders') }}
