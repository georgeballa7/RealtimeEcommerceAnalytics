select order_date, source_system, is_synthetic, count(*) as duplicates
from {{ ref('mart_daily_sales') }}
group by 1,2,3
having count(*) > 1
