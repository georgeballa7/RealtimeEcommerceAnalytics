from decimal import Decimal
import pytest
from ecommerce.application.use_cases.shopify_price_reconciliation import reconcile_targets
from ecommerce.infrastructure.shopify.variant_pricing import ShopifyVariantPricing
from ecommerce.infrastructure.shopify.client import ShopifyError


class FakeGraphQL:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def query(self, doc, variables, mutation=False):
        self.calls.append((doc, variables, mutation))
        return self.response


def test_variant_read_checks_identity():
    api = FakeGraphQL({"productVariant": {
        "id": "gid://shopify/ProductVariant/2", "sku": "A", "price": "2.15",
        "product": {"id": "gid://shopify/Product/1"}}})
    assert ShopifyVariantPricing(api).read(
        sku="A", product_gid="gid://shopify/Product/1",
        variant_gid="gid://shopify/ProductVariant/2") == Decimal("2.15")
    assert not api.calls[0][2]


def test_variant_read_rejects_wrong_sku():
    api = FakeGraphQL({"productVariant": {"id": "gid://shopify/ProductVariant/2",
        "sku": "OTHER", "price": "2.15", "product": {"id": "gid://shopify/Product/1"}}})
    with pytest.raises(ShopifyError):
        ShopifyVariantPricing(api).read(
            sku="A", product_gid="gid://shopify/Product/1",
            variant_gid="gid://shopify/ProductVariant/2")


def test_update_is_explicit_and_requires_read_back():
    api = FakeGraphQL({"productVariantsBulkUpdate": {
        "product": {"id": "gid://shopify/Product/1"},
        "productVariants": [{"id": "gid://shopify/ProductVariant/2", "sku": "A"}],
        "userErrors": []}})
    ShopifyVariantPricing(api).update(sku="A", product_gid="gid://shopify/Product/1",
        variant_gid="gid://shopify/ProductVariant/2", price=Decimal("2.15"))
    assert api.calls[0][2]
    assert api.calls[0][1]["variants"][0]["price"] == "2.15"


def test_reconcile_fails_closed_when_population_incomplete():
    with pytest.raises(ValueError):
        reconcile_targets({"schema_version":"phase5-gbp-sku-prices-v1","currency":"GBP",
                           "mode":"DRY_RUN","count":3408,"prices":[]}, [])
