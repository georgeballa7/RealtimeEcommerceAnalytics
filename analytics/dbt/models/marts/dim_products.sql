-- One row per source-qualified product variant.
-- Shopify product_key remains NULL until the verified catalog mapping is joined.
-- This avoids fabricating a relationship to a historical stock code.
select
    variant_key,
    max(product_key) as product_key,
    max(sku) as sku,
    max(source_system) as source_system
from {{ ref('fct_order_lines') }}
where variant_key is not null
group by variant_key
