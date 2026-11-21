-- Shopify Silver has already passed the Glue verified-identity quality gate.
-- Recheck the mapping rather than falling back to an unverified Shopify GID.
select
    'shopify:' || o.shop_domain || ':' || cast(o.shopify_order_id as varchar(30)) as order_key,
    cast(o.shopify_order_id as varchar(40)) as source_order_id,
    cast(i.customer_id as varchar(255)) as customer_key,
    o.created_at_source as order_timestamp_utc,
    o.gross_total as gross_gbp,
    o.current_total as current_gbp,
    o.cancelled_at,
    o.financial_status,
    'shopify_live' as source_system,
    o.synthetic as is_synthetic,
    o.currency,
    cast(null as varchar(100)) as batch_id
from {{ source('silver_staging', 'shopify_live_order') }} o
left join {{ source('silver_staging', 'shopify_customer_identity') }} i
  on o.shopify_customer_gid = i.shopify_customer_gid
 and i.status = 'verified'
where o.currency = 'GBP'
