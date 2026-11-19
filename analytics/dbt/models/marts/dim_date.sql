with digits as (
  select 0 as n union all select 1 union all select 2 union all select 3
  union all select 4 union all select 5 union all select 6 union all select 7
  union all select 8 union all select 9
),
seq as (
  select a.n + b.n*10 + c.n*100 as n
  from digits a cross join digits b cross join digits c
)
select
  cast(dateadd(day, n, '2026-01-01'::date) as date) as date_key,
  extract(year from dateadd(day,n,'2026-01-01'::date)) as year_number,
  extract(month from dateadd(day,n,'2026-01-01'::date)) as month_number,
  extract(dow from dateadd(day,n,'2026-01-01'::date)) as weekday_number
from seq
where n < 730
