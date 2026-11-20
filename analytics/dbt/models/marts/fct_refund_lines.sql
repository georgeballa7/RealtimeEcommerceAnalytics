-- One operational recorded refund line; not verified payment settlement.
-- For Power BI we add customer/date directly instead of chaining fact-to-fact.
with refunds as (
    select
        'shopify:' || r.shop_domain || ':' || cast(r.shopify_refund_id as varchar(30))
        || ':' || cast(rl.shopify_refund_line_id as varchar(30)) as refund_line_key,
        'shopify:' || r.shop_domain || ':' || cast(r.shopify_order_id as varchar(30)) as order_key,
        case when rl.shopify_order_line_id is not null
          then 'shopify:' || r.shop_domain || ':' || cast(rl.shopify_order_line_id as varchar(30))
          else null end as line_key,
        r.created_at_source as refund_created_at,
        cast(r.created_at_source as date) as refund_date_key,
        rl.quantity as refund_quantity,
        rl.subtotal as recorded_refund_line_subtotal_gbp,
        'not_payment_settlement' as financial_semantics
    from {{ source('silver_staging','shopify_live_refund') }} r
    join {{ source('silver_staging','shopify_live_refund_line') }} rl
      on r.shop_domain = rl.shop_domain and r.shopify_refund_id = rl.shopify_refund_id
)
select
    r.*, o.customer_key, l.variant_key
from refunds r
left join {{ ref('fct_orders') }} o on r.order_key = o.order_key
left join {{ ref('fct_order_lines') }} l on r.line_key = l.line_key
