-- Reject an empty/partial historical baseline; live Shopify is optional until loaded.
with actual as (
    select
      (select count(*) from {{ source('silver_staging', 'synthetic_orders') }}) as orders,
      (select count(*) from {{ source('silver_staging', 'synthetic_order_items') }}) as lines,
      (select count(*) from {{ source('silver_staging', 'synthetic_cancellations') }}) as cancellations
)
select *
from actual
where orders < 14110 or lines < 173905 or cancellations < 1335
