select
    'synthetic_2026:' || cast(order_id as varchar(40)) as order_key,
    cast(order_id as varchar(40)) as source_order_id,
    cast(customer_id as varchar(255)) as customer_key,
    event_time_utc as order_timestamp_utc,
    cast(order_total as decimal(18,2)) as gross_gbp,
    cast(order_total as decimal(18,2)) as current_gbp,
    cast(null as timestamp) as cancelled_at,
    cast(null as varchar(80)) as financial_status,
    'synthetic_2026' as source_system,
    true as is_synthetic,
    currency,
    batch_id
from {{ source('silver_staging', 'synthetic_orders') }}
where synthetic = true and currency = 'GBP'
