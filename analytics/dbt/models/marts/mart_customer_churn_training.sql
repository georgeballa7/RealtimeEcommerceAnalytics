-- Monthly as-of examples. Every label uses a *future* 30-day period.
-- No evaluation sample is created if full label horizon is not observable.
with month_index as (
  select 0 as n union all select 1 union all select 2 union all select 3
  union all select 4 union all select 5 union all select 6 union all select 7
  union all select 8 union all select 9 union all select 10
),
asof_dates as (
  select dateadd(month, n, '2026-03-01'::date) as as_of_date from month_index
),
last_observed as (
  select max(order_timestamp_utc) as last_order_time from {{ ref('fct_orders') }}
),
eligible_months as (
  select a.as_of_date
  from asof_dates a cross join last_observed l
  where dateadd(day,30,a.as_of_date) <= l.last_order_time
),
customers as (
  select distinct customer_key
  from {{ ref('fct_orders') }}
  where customer_key is not null
),
panel as (
  select c.customer_key, a.as_of_date
  from customers c cross join eligible_months a
  where exists (
    select 1 from {{ ref('fct_orders') }} o
    where o.customer_key=c.customer_key
      and o.order_timestamp_utc < a.as_of_date
  )
),
features as (
  select
    p.customer_key, p.as_of_date,
    count(case when o.order_timestamp_utc >= dateadd(day,-90,p.as_of_date)
               and o.order_timestamp_utc < p.as_of_date then 1 end) as order_count_90d,
    coalesce(sum(case when o.order_timestamp_utc >= dateadd(day,-90,p.as_of_date)
                      and o.order_timestamp_utc < p.as_of_date then o.current_gbp else 0 end),0)
       as spend_90d_gbp,
    count(case when o.order_timestamp_utc < p.as_of_date then 1 end) as lifetime_order_count,
    -- Both DATEDIFF operands must use the same temporal type in Redshift.
    -- Recency is a calendar-day feature, so explicit DATE operands also
    -- avoid a TIMESTAMPTZ versus TIMESTAMP overload error.
    datediff(day,
             cast(max(case when o.order_timestamp_utc < p.as_of_date
                           then o.order_timestamp_utc end) as date),
             cast(p.as_of_date as date)) as recency_days,
    count(case when o.order_timestamp_utc >= p.as_of_date
               and o.order_timestamp_utc < dateadd(day,30,p.as_of_date) then 1 end)
       as orders_in_next_30d
  from panel p
  left join {{ ref('fct_orders') }} o on o.customer_key=p.customer_key
  group by p.customer_key,p.as_of_date
)
select
    customer_key, as_of_date, order_count_90d, spend_90d_gbp,
    lifetime_order_count, recency_days,
    case when orders_in_next_30d = 0 then 1 else 0 end as churn_next_30d
from features
where recency_days is not null
