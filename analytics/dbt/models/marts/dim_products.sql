-- One row per source-qualified product variant in the verified Silver.
-- Never merge Shopify and historical products on SKU alone.
-- Cross-source mapping requires the catalog identity-ledger export.
with variants as (
    select
        cast(variant_id as varchar(255)) as variant_key,
        cast(product_id as varchar(255)) as product_key,
        source_stock_code as sku,
        'synthetic_2026' as source_system
    from {{ source('silver_staging', 'synthetic_order_items') }}
    where variant_id is not null

    union all

    select
        case
          when shopify_variant_id is not null then
            'shopify:' || shop_domain || ':variant:'
              || cast(shopify_variant_id as varchar(30))
          else
            'shopify:' || shop_domain || ':unmapped-line:'
              || cast(shopify_order_id as varchar(30)) || ':'
              || cast(shopify_line_id as varchar(30))
        end as variant_key,
        cast(null as varchar(255)) as product_key,
        sku,
        'shopify_live' as source_system
    from {{ source('silver_staging', 'shopify_live_order_line') }}
),
grouped as (
    select variant_key,
           min(product_key) as product_key,
           min(sku) as sku,
           min(source_system) as source_system
    from variants
    where variant_key is not null
    group by 1
)
select variant_key, product_key, sku, source_system
from grouped
