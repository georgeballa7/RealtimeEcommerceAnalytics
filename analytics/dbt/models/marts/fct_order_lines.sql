-- One row per order item, joined to orders only for conformed date/customer FKs.
with lines as (
    select
      'synthetic_2026:' || cast(i.order_item_id as varchar(40)) as line_key,
      'synthetic_2026:' || cast(i.order_id as varchar(40)) as order_key,
      cast(i.variant_id as varchar(255)) as variant_key,
      cast(i.product_id as varchar(255)) as product_key,
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
      case
        when i.shopify_variant_id is not null then
          'shopify:' || i.shop_domain || ':variant:' || cast(i.shopify_variant_id as varchar(30))
        else
          'shopify:' || i.shop_domain || ':unmapped-line:' || cast(i.shopify_order_id as varchar(30))
          || ':' || cast(i.shopify_line_id as varchar(30))
      end as variant_key,
      cast(null as varchar(255)) as product_key,
      i.sku,
      i.quantity,
      i.unit_price as unit_price_gbp,
      (i.quantity * i.unit_price - coalesce(i.discount_total,0)) as gross_line_gbp,
      'shopify_live' as source_system
    from {{ source('silver_staging','shopify_live_order_line') }} i
)
select
    l.line_key, l.order_key, l.variant_key, l.product_key, l.sku,
    l.quantity, l.unit_price_gbp, l.gross_line_gbp, l.source_system,
    o.customer_key, o.order_date_key
from lines l
left join {{ ref('fct_orders') }} o on l.order_key = o.order_key
