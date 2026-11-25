-- Unlabeled current features: safe for inference, never used for training.
with asof as (
  select dateadd(day, 1, cast(max(order_timestamp_utc) as date)) as as_of_date
  from {{ ref('fct_orders') }}
),
orders as (
  select customer_key, order_timestamp_utc, current_gbp
  from {{ ref('fct_orders') }}
  where customer_key is not null
)
select
    o.customer_key,
    a.as_of_date,
    sum(case when o.order_timestamp_utc >= dateadd(day,-90,a.as_of_date)
             then 1 else 0 end) as order_count_90d,
    sum(case when o.order_timestamp_utc >= dateadd(day,-90,a.as_of_date)
             then o.current_gbp else 0 end) as spend_90d_gbp,
    count(*) as lifetime_order_count,
    -- Cast both arguments to DATE for the same calendar-day semantics
    -- as churn training and to avoid TIMESTAMPTZ/TIMESTAMP mismatch.
    datediff(day, cast(max(o.order_timestamp_utc) as date),
             cast(a.as_of_date as date)) as recency_days
from orders o cross join asof a
where o.order_timestamp_utc < a.as_of_date
group by o.customer_key,a.as_of_date
