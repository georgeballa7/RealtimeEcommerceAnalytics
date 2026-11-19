"""Pure read-only pricing plan for verified Shopify GBP variants."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re

_PENNY = Decimal("0.01")
_PRODUCT = re.compile(r"gid://shopify/Product/[0-9]+\\Z")
_VARIANT = re.compile(r"gid://shopify/ProductVariant/[0-9]+\\Z")


@dataclass(frozen=True)
class VerifiedVariant:
    sku: str
    product_gid: str
    variant_gid: str


@dataclass(frozen=True)
class PriceChange:
    sku: str
    product_gid: str
    variant_gid: str
    currency: str
    price: str


def build_gbp_price_plan(
    sku_prices: dict[str, str],
    verified_variants: list[VerifiedVariant],
    approved_skus: frozenset[str],
    *,
    shop_currency: str,
    expected_count: int = 3408,
) -> tuple[PriceChange, ...]:
    """Fail closed unless every approved SKU maps once to a valid Shopify variant."""
    if shop_currency != "GBP":
        raise ValueError("Shopify base currency must be GBP")
    if len(approved_skus) != expected_count:
        raise ValueError("Approval population differs from expected count")
    if set(sku_prices) != approved_skus:
        raise ValueError("Prices must match approved SKU set exactly")
    if len(verified_variants) != expected_count:
        raise ValueError("Mapping population differs from expected count")
    mapping: dict[str, VerifiedVariant] = {}
    seen_products: set[str] = set()
    seen_variants: set[str] = set()
    for row in verified_variants:
        if (
            row.sku not in approved_skus or row.sku in mapping
            or not _PRODUCT.fullmatch(row.product_gid)
            or not _VARIANT.fullmatch(row.variant_gid)
            or row.product_gid in seen_products
            or row.variant_gid in seen_variants
        ):
            raise ValueError("Unapproved, duplicate, or invalid verified mapping")
        mapping[row.sku] = row
        seen_products.add(row.product_gid)
        seen_variants.add(row.variant_gid)
    if set(mapping) != approved_skus:
        raise ValueError("Missing approved Shopify variant mappings")
    rows = []
    for sku in sorted(approved_skus):
        try:
            amount = Decimal(sku_prices[sku])
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("Invalid GBP price") from exc
        if not amount.is_finite() or amount <= 0 or amount != amount.quantize(_PENNY):
            raise ValueError("Price must be positive with at most 2 decimal places")
        row = mapping[sku]
        rows.append(PriceChange(
            sku=sku, product_gid=row.product_gid, variant_gid=row.variant_gid,
            currency="GBP", price=f"{amount:.2f}",
        ))
    return tuple(rows)


def canonical_manifest_bytes(rows: tuple[PriceChange, ...]) -> bytes:
    payload = {
        "schema_version": "shopify-gbp-price-plan-v1",
        "mode": "DRY_RUN",
        "currency": "GBP",
        "changes": [
            {
                "sku": x.sku, "product_gid": x.product_gid,
                "variant_gid": x.variant_gid, "currency": x.currency,
                "price": x.price,
            }
            for x in rows
        ],
    }
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def manifest_sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
