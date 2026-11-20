-- One variant must not resolve to multiple canonical products within a load.
select variant_key, count(distinct product_key) as different_products
from {{ ref('fct_order_lines') }}
where variant_key is not null and product_key is not null
group by 1
having count(distinct product_key) > 1
