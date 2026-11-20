-- The operational source permits 0, but zero-quantity sales are not valid facts.
select line_key, order_key, quantity
from {{ ref('fct_order_lines') }}
where quantity is null or quantity <= 0
