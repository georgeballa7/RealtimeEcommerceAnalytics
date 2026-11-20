select
  'synthetic_2026:' || event_id as cancellation_key,
  'synthetic_2026:' || order_id as order_key,
  'synthetic_2026:' || order_item_id as line_key,
  event_time_utc as cancelled_at,
  cancelled_quantity,
  cancelled_amount as cancelled_merchandise_gbp,
  financial_semantics,
  true as is_simulated
from {{ source('silver_staging','synthetic_cancellations') }}
where provenance = 'simulated_cancellation_not_verified_refund'
