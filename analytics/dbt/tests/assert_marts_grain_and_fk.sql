-- dbt relationships are checked in _schema.yml. This operational acceptance
-- test also catches missing source-conformed customers and product variants.
with failures as (
    select 'order_customer_orphan' as check_name, count(*) as n
    from {{ ref('fct_orders') }} o
    left join {{ ref('dim_customers') }} c on o.customer_key = c.customer_key
    where c.customer_key is null

    union all
    select 'line_variant_orphan', count(*)
    from {{ ref('fct_order_lines') }} l
    left join {{ ref('dim_products') }} p on l.variant_key = p.variant_key
    where p.variant_key is null

    union all
    select 'order_date_orphan', count(*)
    from {{ ref('fct_orders') }} o
    left join {{ ref('dim_date') }} d on o.order_date_key = d.date_key
    where d.date_key is null

    union all
    select 'order_line_orphan', count(*)
    from {{ ref('fct_order_lines') }} l
    left join {{ ref('fct_orders') }} o on l.order_key = o.order_key
    where o.order_key is null
)
select * from failures where n > 0
