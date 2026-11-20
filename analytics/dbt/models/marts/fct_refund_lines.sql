select
    'shopify:' || r.shop_domain || ':' || cast(r.shopify_refund_id as varchar(30)) || ':' ||
      cast(rl.shopify_refund_line_id as varchar(30)) as refund_line_key,
    'shopify:' || r.shop_domain || ':' || cast(r.shopify_order_id as varchar(30)) as order_key,
    r.created_at_source as refund_created_at,
    rl.quantity as refund_quantity,
    rl.subtotal as recorded_refund_line_subtotal_gbp,
    'not_payment_settlement' as financial_semantics
from {{ source('raw','shopify_live_refund') }} r
join {{ source('raw','shopify_live_refund_line') }} rl
  on r.shop_domain = rl.shop_domain and r.shopify_refund_id = rl.shopify_refund_id
