-- Minimum reconciliation gates for the loaded 20-Nov-2026 foundation.
-- These intentionally allow later snapshots to increase live counts.
with counts as (
    select
      (select count(*) from {{ source('silver_staging', 'shopify_live_order') }}) as live_orders,
      (select count(*) from {{ source('silver_staging', 'shopify_live_order_line') }}) as live_lines,
      (select count(*) from {{ source('silver_staging', 'shopify_customer_identity') }}) as identities,
      (select count(*) from {{ source('silver_staging', 'warehouse_load_ledger') }}
       where source_type = 'shopify_silver') as silver_loads
)
select * from counts
where live_orders < 196 or live_lines < 403 or identities < 5942
   or silver_loads < 2
