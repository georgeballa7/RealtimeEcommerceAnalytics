from uuid import UUID

import pytest

from ecommerce.domain.identifiers import (
    canonical_id,
    historical_customer_id,
    historical_order_id,
    historical_order_item_id,
    historical_product_id,
)


def test_historical_customer_id_is_deterministic() -> None:
    customer_id = historical_customer_id('17850')
    assert customer_id == historical_customer_id('17850')
    assert UUID(customer_id).version == 5


def test_missing_customer_remains_anonymous() -> None:
    assert historical_customer_id(None) is None
    assert historical_customer_id('') is None


def test_namespaces_prevent_accidental_collisions() -> None:
    assert historical_order_id('17850') != historical_customer_id('17850')
    assert historical_product_id('17850') != historical_customer_id('17850')
    assert historical_order_id('536365') != canonical_id('order', 'shopify', '536365')


def test_source_rows_remain_distinct_even_when_invoice_lines_repeat() -> None:
    assert historical_order_item_id('Year 2009-2010:1') != historical_order_item_id('Year 2009-2010:2')


def test_invalid_identity_is_rejected() -> None:
    with pytest.raises(ValueError):
        canonical_id('order', 'uci_online_retail_ii', '  ')
