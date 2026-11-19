import pytest

from ecommerce.application.use_cases.shopify_gbp_pricing import (
    VerifiedVariant, build_gbp_price_plan, canonical_manifest_bytes, manifest_sha256,
)


def mapping(sku="A"):
    return VerifiedVariant(sku, "gid://shopify/Product/100", "gid://shopify/ProductVariant/200")


def test_deterministic_gbp_plan():
    a = build_gbp_price_plan({"A": "3.50"}, [mapping()], frozenset({"A"}),
                             shop_currency="GBP", expected_count=1)
    b = build_gbp_price_plan({"A": "3.50"}, [mapping()], frozenset({"A"}),
                             shop_currency="GBP", expected_count=1)
    assert a == b
    assert a[0].price == "3.50"
    assert manifest_sha256(canonical_manifest_bytes(a)) == manifest_sha256(canonical_manifest_bytes(b))


@pytest.mark.parametrize("prices,rows,currency", [
    ({"A": "0.00"}, [mapping()], "GBP"),
    ({"A": "3.501"}, [mapping()], "GBP"),
    ({"A": "-1"}, [mapping()], "GBP"),
    ({"A": "NaN"}, [mapping()], "GBP"),
    ({"A": "3.50"}, [mapping()], "USD"),
    ({"A": "3.50"}, [], "GBP"),
    ({"A": "3.50"}, [mapping(), mapping()], "GBP"),
])
def test_fail_closed(prices, rows, currency):
    with pytest.raises(ValueError):
        build_gbp_price_plan(prices, rows, frozenset({"A"}),
                             shop_currency=currency, expected_count=1)
