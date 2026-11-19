with observed as (
  select customer_key,
         min(order_timestamp_utc) as first_seen_at,
         max(order_timestamp_utc) as last_seen_at,
         count(*) as total_orders,
         sum(current_gbp) as lifetime_order_value_gbp
  from {{ ref('fct_orders') }}
  where customer_key is not null
  group by 1
)
select customer_key, first_seen_at, last_seen_at, total_orders,
       lifetime_order_value_gbp
from observed
