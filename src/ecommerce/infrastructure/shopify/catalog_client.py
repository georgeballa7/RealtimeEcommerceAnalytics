"""Shopify 2026-07 catalog adapter using existing authenticated GraphQL transport.

An externally defined unique PRODUCT custom-ID metafield must exist before
use. No price, inventory, currency or fabricated Shopify identities are set.
Mutation results are always reconciled through independent read-back.
"""
from __future__ import annotations

import re
from uuid import UUID

from ecommerce.application.use_cases.catalog_selection import CatalogCandidate
from ecommerce.infrastructure.shopify.client import ShopifyError

PRODUCT_ID_KEY = "ecommerce_canonical_product_id"

PRODUCT_DEFINITION = """query CanonicalProductDefinition($namespace: String!, $key: String!) {
  metafieldDefinitions(first: 50, ownerType: PRODUCT, namespace: $namespace, key: $key) {
    nodes { id namespace key type { name } }
  }
}"""
PRODUCT_BY_ID = """query ProductByCanonicalId($identifier: ProductIdentifierInput!) {
  productByIdentifier(identifier: $identifier) { id }
}"""
PRODUCT_SET = """mutation CreateHistoricalProduct($identifier: ProductSetIdentifiers,
  $input: ProductSetInput!) {
  productSet(identifier: $identifier, input: $input, synchronous: true) {
    product { id }
    userErrors { code field }
  }
}"""
PRODUCT_VARIANTS = """query ProductDefaultVariant($id: ID!) {
  product(id: $id) {
    id
    variants(first: 3) { nodes { id sku title } }
  }
}"""
VARIANT_UPDATE = """mutation UpdateHistoricalDefaultVariant(
  $productId: ID!, $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    product { id }
    productVariants { id sku }
    userErrors { code field }
  }
}"""


def _validate_gid(value: object, typename: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        rf"gid://shopify/{typename}/[0-9]+", value
    ):
        raise ShopifyError(f"Invalid Shopify {typename} GID")
    return value


def _raise_user_errors(result: dict, operation: str):
    if result.get("userErrors"):
        # Codes are allowlisted, never include message, field values or payloads.
        safe = []
        for error in result["userErrors"][:5]:
            code = error.get("code") if isinstance(error, dict) else None
            safe.append(
                code if isinstance(code, str) and re.fullmatch(
                    r"[A-Z][A-Z0-9_]{0,63}", code
                ) else "UNKNOWN"
            )
        raise ShopifyError(f"Shopify {operation} rejected: {','.join(safe)}")


class ShopifyCatalogClient:
    def __init__(self, graphql, *, namespace: str = "custom",
                 product_key: str = PRODUCT_ID_KEY):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", namespace):
            raise ValueError("Invalid catalog metafield namespace")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", product_key):
            raise ValueError("Invalid catalog metafield key")
        self.graphql = graphql
        self.namespace = namespace
        self.product_key = product_key

    def verify_product_definition(self):
        response = self.graphql.query(
            PRODUCT_DEFINITION,
            {"namespace": self.namespace, "key": self.product_key},
        )
        nodes = ((response.get("metafieldDefinitions") or {}).get("nodes") or [])
        matches = [
            item for item in nodes if item.get("namespace") == self.namespace
            and item.get("key") == self.product_key
            and (item.get("type") or {}).get("name") == "id"
        ]
        if len(matches) != 1:
            raise ShopifyError("Missing or ambiguous PRODUCT custom ID definition")

    def _identifier(self, product_id: str) -> dict:
        try:
            valid = str(UUID(product_id)) == product_id
        except (TypeError, AttributeError, ValueError):
            valid = False
        if not valid:
            raise ValueError("Invalid canonical product UUID")
        return {
            "customId": {
                "namespace": self.namespace,
                "key": self.product_key,
                "value": product_id,
            }
        }

    def lookup_product(self, product_id: str) -> str | None:
        product = self.graphql.query(
            PRODUCT_BY_ID, {"identifier": self._identifier(product_id)}
        ).get("productByIdentifier")
        if product is None:
            return None
        return _validate_gid(product.get("id"), "Product")

    def create_product(self, candidate: CatalogCandidate) -> str:
        # A draft keeps an historical catalog from being accidentally published.
        result = self.graphql.query(
            PRODUCT_SET,
            {
                "identifier": self._identifier(candidate.product_id),
                "input": {"title": candidate.description, "status": "DRAFT"},
            },
            mutation=True,
        ).get("productSet") or {}
        _raise_user_errors(result, "productSet")
        return _validate_gid((result.get("product") or {}).get("id"), "Product")

    def _variants(self, product_gid: str) -> list[dict]:
        _validate_gid(product_gid, "Product")
        product = self.graphql.query(
            PRODUCT_VARIANTS, {"id": product_gid}
        ).get("product")
        if not product or product.get("id") != product_gid:
            raise ShopifyError("Expected Shopify product not found")
        nodes = ((product.get("variants") or {}).get("nodes") or [])
        if not isinstance(nodes, list) or len(nodes) > 2:
            raise ShopifyError("Unexpected Shopify product variant configuration")
        for node in nodes:
            _validate_gid(node.get("id"), "ProductVariant")
        return nodes

    def lookup_variant(self, product_gid: str, sku: str) -> str | None:
        matching = [
            node for node in self._variants(product_gid)
            if node.get("sku") == sku
        ]
        if len(matching) > 1:
            raise ShopifyError("Duplicate Shopify variant SKUs on product")
        return matching[0]["id"] if matching else None

    def create_variant(self, product_gid: str, candidate: CatalogCandidate) -> str:
        # One historical product maps to one variant. Reuse Shopify's initial
        # empty default variant rather than creating a second unmanaged variant.
        variants = self._variants(product_gid)
        if len(variants) != 1 or variants[0].get("sku") not in (None, ""):
            raise ShopifyError("Default variant cannot be safely reconciled")
        if variants[0].get("title") != "Default Title":
            raise ShopifyError("Unexpected default Shopify variant title")
        result = self.graphql.query(
            VARIANT_UPDATE,
            {
                "productId": product_gid,
                "variants": [{
                    "id": variants[0]["id"],
                    "inventoryItem": {"sku": candidate.sku},
                }],
            },
            mutation=True,
        ).get("productVariantsBulkUpdate") or {}
        _raise_user_errors(result, "productVariantsBulkUpdate")
        product = result.get("product") or {}
        if product.get("id") != product_gid:
            raise ShopifyError("Variant update returned wrong product")
        nodes = result.get("productVariants") or []
        if len(nodes) != 1 or nodes[0].get("sku") != candidate.sku:
            raise ShopifyError("Variant update returned unexpected payload")
        if nodes[0].get("id") != variants[0]["id"]:
            raise ShopifyError("Variant update replaced original default variant")
        return _validate_gid(nodes[0].get("id"), "ProductVariant")
