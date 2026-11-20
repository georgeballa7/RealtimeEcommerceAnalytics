with product_source as (
  select
    cast(variant_id as varchar(255)) as variant_key,
    cast(product_id as varchar(255)) as product_key,
    source_stock_code as sku
  from {{ source('silver_staging','synthetic_order_items') }}
),
ranked as (
  select distinct variant_key, product_key, sku from product_source
)
select * from ranked
