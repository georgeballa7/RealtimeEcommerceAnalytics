-- During historical-only bootstrap, empty Shopify Silver is expected.
-- After first successful Glue+Redshift sync, turn on the required-live gate:
-- dbt build --vars '{"require_live_shopify_orders": true}'
{% if var('require_live_shopify_orders', false) %}
select 'shopify_live_order_missing' as validation_error
where not exists (
    select 1 from {{ source('silver_staging', 'shopify_live_order') }}
)
{% else %}
select 'not_enabled' as validation_error where 1 = 0
{% endif %}
