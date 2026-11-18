"""Offline GraphQL contract tests for historical catalog synchronization."""
from uuid import uuid4

import pytest

from ecommerce.application.use_cases.catalog_selection import CatalogCandidate
from ecommerce.infrastructure.shopify.catalog_client import ShopifyCatalogClient
from ecommerce.infrastructure.shopify.client import ShopifyError


class GraphQL:
    def __init__(self):
        self.calls = []
        self.product = None
        self.variants = []

    def query(self, document, variables=None, *, mutation=False):
        self.calls.append((document, variables, mutation))
        if "metafieldDefinitions" in document:
            return {"metafieldDefinitions": {"nodes": [
                {
                    "namespace": "custom", "key": "ecommerce_canonical_product_id",
                    "type": {"name": "id"},
                }
            ]}}
        if "productByIdentifier" in document:
            return {"productByIdentifier": (
                {"id": self.product} if self.product else None
            )}
        if "productSet(" in document:
            self.product = "gid://shopify/Product/77"
            return {"productSet": {
                "product": {"id": self.product}, "userErrors": [],
            }}
        if "productVariantsBulkUpdate" in document:
            assert mutation
            data = variables["variants"][0]
            self.variants = [{
                "id": data["id"], "sku": data["sku"], "title": "Default Title",
            }]
            return {"productVariantsBulkUpdate": {
                "productVariants": self.variants, "userErrors": [],
            }}
        if "product(id:" in document:
            return {"product": {
                "id": self.product,
                "variants": {"nodes": self.variants},
            }}
        raise AssertionError("Unexpected Shopify GraphQL operation")


def item():
    return CatalogCandidate(
        str(uuid4()), str(uuid4()), "85123A", "85123A", "Historical item"
    )


def test_custom_id_definition_verified():
    client = ShopifyCatalogClient(GraphQL())
    client.verify_product_definition()


def test_product_lookup_uses_canonical_custom_id():
    transport = GraphQL()
    c = item()
    client = ShopifyCatalogClient(transport)
    assert client.lookup_product(c.product_id) is None
    variables = transport.calls[0][1]
    assert variables["identifier"]["customId"]["value"] == c.product_id
    assert variables["identifier"]["customId"]["key"] == "ecommerce_canonical_product_id"


def test_product_set_is_draft_and_leaves_price_currency_unset():
    transport = GraphQL()
    client = ShopifyCatalogClient(transport)
    c = item()
    assert client.create_product(c) == "gid://shopify/Product/77"
    query, variables, mutation = transport.calls[0]
    assert mutation
    assert "productSet(" in query
    assert variables["input"] == {"title": "Historical item", "status": "DRAFT"}
    assert "price" not in str(variables).lower()
    assert variables["identifier"]["customId"]["value"] == c.product_id


def test_existing_default_variant_is_reused_without_new_variant():
    transport = GraphQL()
    transport.product = "gid://shopify/Product/77"
    transport.variants = [{
        "id": "gid://shopify/ProductVariant/8",
        "sku": "", "title": "Default Title",
    }]
    client = ShopifyCatalogClient(transport)
    c = item()
    assert client.lookup_variant(transport.product, c.sku) is None
    assert client.create_variant(transport.product, c) == "gid://shopify/ProductVariant/8"
    assert client.lookup_variant(transport.product, c.sku) == "gid://shopify/ProductVariant/8"
    update_calls = [x for x in transport.calls if "productVariantsBulkUpdate" in x[0]]
    assert len(update_calls) == 1
    assert update_calls[0][1]["variants"] == [
        {"id": "gid://shopify/ProductVariant/8", "sku": "85123A"}
    ]


def test_existing_nondefault_variant_blocks_mutation():
    transport = GraphQL()
    transport.product = "gid://shopify/Product/77"
    transport.variants = [{
        "id": "gid://shopify/ProductVariant/8",
        "sku": "OTHER", "title": "Default Title",
    }]
    with pytest.raises(ShopifyError, match="cannot be safely"):
        ShopifyCatalogClient(transport).create_variant(transport.product, item())
    assert not any(x[2] for x in transport.calls)


def test_duplicate_sku_blocks_reconciliation():
    transport = GraphQL()
    transport.product = "gid://shopify/Product/77"
    transport.variants = [
        {"id": "gid://shopify/ProductVariant/8", "sku": "85123A", "title": "A"},
        {"id": "gid://shopify/ProductVariant/9", "sku": "85123A", "title": "B"},
    ]
    with pytest.raises(ShopifyError, match="Duplicate"):
        ShopifyCatalogClient(transport).lookup_variant(transport.product, "85123A")


def test_missing_custom_id_definition_is_rejected():
    transport = GraphQL()
    def no_definition(_document, variables=None, *, mutation=False):
        return {"metafieldDefinitions": {"nodes": []}}

    transport.query = no_definition
    with pytest.raises(ShopifyError, match="PRODUCT custom ID"):
        ShopifyCatalogClient(transport).verify_product_definition()


def test_invalid_product_uuid_never_reaches_shopify():
    transport = GraphQL()
    with pytest.raises(ValueError, match="canonical product UUID"):
        ShopifyCatalogClient(transport).lookup_product("wrong")
    assert transport.calls == []


def test_safe_mutation_error_does_not_leak_message():
    transport = GraphQL()
    def rejected(_document, variables=None, *, mutation=False):
        return {"productSet": {
            "product": None, "userErrors": [{
                "code": "INVALID", "message": "private@example.org",
            }],
        }}

    transport.query = rejected
    with pytest.raises(ShopifyError, match="INVALID") as error:
        ShopifyCatalogClient(transport).create_product(item())
    assert "private@example.org" not in str(error.value)
