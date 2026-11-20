-- One canonical customer per verified ID, whether or not they purchased in 2026.
-- Identity rows from Silver are authoritative; observed synthetic buyers are
-- included as canonical IDs from the already-validated historical backfill.
-- Sales measures belong in facts/marts, not in a slowly changing dimension.
with known_customers as (
    select cast(customer_id as varchar(255)) as customer_key
    from {{ source('silver_staging', 'shopify_customer_identity') }}
    where status = 'verified' and customer_id is not null

    union

    select cast(customer_id as varchar(255)) as customer_key
    from {{ source('silver_staging', 'synthetic_orders') }}
    where customer_id is not null
),
observed as (
    select customer_key,
           min(order_timestamp_utc) as first_order_at,
           max(order_timestamp_utc) as latest_order_at
    from {{ ref('fct_orders') }}
    where customer_key is not null
    group by 1
)
select c.customer_key,
       o.first_order_at,
       o.latest_order_at
from known_customers c
left join observed o on c.customer_key = o.customer_key
