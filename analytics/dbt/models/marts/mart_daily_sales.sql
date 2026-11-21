with orders as (
  select cast(order_timestamp_utc as date) as order_date,
         source_system, is_synthetic, count(*) as orders,
         sum(gross_gbp) as gross_gbp,
         sum(current_gbp) as current_gbp
  from {{ ref('fct_orders') }}
  group by 1,2,3
),
cancels as (
  select cast(cancelled_at as date) as cancelled_date,
         sum(cancelled_merchandise_gbp) as simulated_cancelled_merchandise_gbp
  from {{ ref('fct_cancellations') }}
  group by 1
)
select o.*, coalesce(c.simulated_cancelled_merchandise_gbp, 0) as simulated_cancelled_merchandise_gbp
from orders o
left join cancels c on o.order_date=c.cancelled_date and o.source_system='synthetic_2026'
