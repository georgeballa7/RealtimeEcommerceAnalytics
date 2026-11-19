"""Shopify variant price read-back and explicitly authorized mutation."""
from __future__ import annotations

from decimal import Decimal
from ecommerce.infrastructure.shopify.catalog_client import _raise_user_errors, _validate_gid
from ecommerce.infrastructure.shopify.client import ShopifyError

VARIANT_READ = """query Phase5VariantPrice($id: ID!) {
  productVariant(id: $id) { id sku price product { id } }
}"""
PRICE_UPDATE = """mutation Phase5PriceUpdate($productId: ID!,
  $variants: [ProductVariantsBulkInput!]!) {
  productVariantsBulkUpdate(productId: $productId, variants: $variants) {
    product { id }
    productVariants { id sku price }
    userErrors { code field }
  }
}"""


class ShopifyVariantPricing:
    def __init__(self, graphql):
        self.graphql = graphql

    def read(self, *, sku: str, product_gid: str, variant_gid: str) -> Decimal:
        _validate_gid(product_gid, "Product")
        _validate_gid(variant_gid, "ProductVariant")
        node = self.graphql.query(VARIANT_READ, {"id": variant_gid}).get("productVariant")
        if (not isinstance(node, dict) or node.get("id") != variant_gid
                or node.get("sku") != sku
                or (node.get("product") or {}).get("id") != product_gid):
            raise ShopifyError("Shopify variant identity or SKU mismatch")
        try:
            price = Decimal(str(node["price"]))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise ShopifyError("Invalid Shopify variant price") from exc
        if not price.is_finite() or price < 0:
            raise ShopifyError("Invalid Shopify variant price")
        return price

    def update(self, *, sku: str, product_gid: str, variant_gid: str, price: Decimal):
        _validate_gid(product_gid, "Product")
        _validate_gid(variant_gid, "ProductVariant")
        if not price.is_finite() or price <= 0 or price != price.quantize(Decimal("0.01")):
            raise ValueError("Invalid price for Shopify write")
        result = self.graphql.query(PRICE_UPDATE, {
            "productId": product_gid,
            "variants": [{"id": variant_gid, "price": f"{price:.2f}"}],
        }, mutation=True).get("productVariantsBulkUpdate") or {}
        _raise_user_errors(result, "productVariantsBulkUpdate")
        if (result.get("product") or {}).get("id") != product_gid:
            raise ShopifyError("Shopify update product mismatch")
        variants = result.get("productVariants") or []
        if len(variants) != 1 or variants[0].get("id") != variant_gid or variants[0].get("sku") != sku:
            raise ShopifyError("Shopify update variant mismatch")
        # This response is NOT proof of persistence; caller must independently read back.
