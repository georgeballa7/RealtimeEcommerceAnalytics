select
  'synthetic_2026:' || i.order_item_id as line_key,
  'synthetic_2026:' || i.order_id as order_key,
  cast(i.variant_id as varchar(255)) as variant_key,
  i.source_stock_code as sku,
  i.quantity,
  i.unit_price as unit_price_gbp,
  i.line_amount as gross_line_gbp,
  'synthetic_2026' as source_system
from {{ source('silver_staging','synthetic_order_items') }} i
union all
select
  'shopify:' || i.shop_domain || ':' || cast(i.shopify_line_id as varchar(30)) as line_key,
  'shopify:' || i.shop_domain || ':' || cast(i.shopify_order_id as varchar(30)) as order_key,
  'shopify:' || cast(i.shopify_variant_id as varchar(30)) as variant_key,
  i.sku,
  i.quantity,
  i.unit_price as unit_price_gbp,
  (i.quantity * i.unit_price - coalesce(i.discount_total,0)) as gross_line_gbp,
  'shopify_live' as source_system
from {{ source('silver_staging','shopify_live_order_line') }} i
