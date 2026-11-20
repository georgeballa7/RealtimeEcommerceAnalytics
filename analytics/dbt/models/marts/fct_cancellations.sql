-- One simulated cancellation event per row.
-- These amounts are NOT verified payments or actual Shopify refunds.
with events as (
    select
      'synthetic_2026:' || event_id as cancellation_key,
      'synthetic_2026:' || order_id as order_key,
      'synthetic_2026:' || order_item_id as line_key,
      event_time_utc as cancelled_at,
      cast(event_time_utc as date) as cancellation_date_key,
      cancelled_quantity,
      cancelled_amount as cancelled_merchandise_gbp,
      financial_semantics,
      true as is_simulated
    from {{ source('silver_staging','synthetic_cancellations') }}
    where provenance = 'simulated_cancellation_not_verified_refund'
)
select
    e.*, o.customer_key, l.variant_key
from events e
left join {{ ref('fct_orders') }} o on e.order_key = o.order_key
left join {{ ref('fct_order_lines') }} l on e.line_key = l.line_key
