select order_key, gross_gbp, current_gbp
from {{ ref('fct_orders') }}
where gross_gbp < 0 or current_gbp < 0
