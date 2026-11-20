select customer_key, as_of_date, count(*) as duplicates
from {{ ref('mart_customer_churn_training') }}
group by 1,2
having count(*) > 1
