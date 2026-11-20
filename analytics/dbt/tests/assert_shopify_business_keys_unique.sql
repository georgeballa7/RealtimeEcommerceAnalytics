-- Redshift does not enforce a composite source PK; check all five Shopify grains.
with violations as (
    select 'shopify_live_order' as relation, count(*) as n
    from (
      select shop_domain, shopify_order_id
      from {{ source('silver_staging', 'shopify_live_order') }}
      group by 1,2 having count(*) > 1
    ) d
    union all
    select 'shopify_live_order_line', count(*)
    from (
      select shop_domain, shopify_order_id, shopify_line_id
      from {{ source('silver_staging', 'shopify_live_order_line') }}
      group by 1,2,3 having count(*) > 1
    ) d
    union all
    select 'shopify_live_refund', count(*)
    from (
      select shop_domain, shopify_refund_id
      from {{ source('silver_staging', 'shopify_live_refund') }}
      group by 1,2 having count(*) > 1
    ) d
    union all
    select 'shopify_live_refund_line', count(*)
    from (
      select shop_domain, shopify_refund_id, shopify_refund_line_id
      from {{ source('silver_staging', 'shopify_live_refund_line') }}
      group by 1,2,3 having count(*) > 1
    ) d
    union all
    select 'shopify_customer_identity', count(*)
    from (
      select shop_gid, customer_id
      from {{ source('silver_staging', 'shopify_customer_identity') }}
      group by 1,2 having count(*) > 1
    ) d
)
select * from violations where n > 0
