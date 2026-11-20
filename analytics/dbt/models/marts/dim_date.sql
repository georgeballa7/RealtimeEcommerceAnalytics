-- Calendar dimension from 2026 through 2040, sufficient for expanding live runs.
with digits as (
  select 0 as n union all select 1 union all select 2 union all select 3
  union all select 4 union all select 5 union all select 6 union all select 7
  union all select 8 union all select 9
),
seq as (
  select a.n + b.n*10 + c.n*100 + d.n*1000 as n
  from digits a cross join digits b cross join digits c cross join digits d
),
dates as (
  select cast(dateadd(day, n, '2026-01-01'::date) as date) as date_key
  from seq
  where dateadd(day, n, '2026-01-01'::date) < '2041-01-01'::date
)
select
  date_key,
  extract(year from date_key) as year_number,
  extract(quarter from date_key) as quarter_number,
  extract(month from date_key) as month_number,
  extract(dow from date_key) as weekday_number
from dates
