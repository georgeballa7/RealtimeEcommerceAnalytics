"""Deterministic manifest-to-RDS-identity reconciliation. No I/O."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID


@dataclass(frozen=True)
class Target:
    sku: str
    variant_id: str
    product_gid: str
    variant_gid: str
    price: Decimal


def reconcile_targets(manifest: dict, mappings: list[tuple[str, str, str, str]]) -> tuple[Target, ...]:
    if (manifest.get("schema_version") != "phase5-gbp-sku-prices-v1"
            or manifest.get("currency") != "GBP" or manifest.get("mode") != "DRY_RUN"
            or manifest.get("count") != 3408):
        raise ValueError("Unexpected manifest")
    rows = manifest.get("prices")
    if not isinstance(rows, list) or len(rows) != 3408 or len(mappings) != 3408:
        raise ValueError("Expected exactly 3408 manifest rows and 3408 mappings")
    from ecommerce.application.use_cases.shopify_gbp_pricing import (
        VerifiedVariant, build_gbp_price_plan,
    )
    skus = [row.get("sku") for row in rows if isinstance(row, dict)]
    if len(skus) != 3408 or len(set(skus)) != 3408:
        raise ValueError("Duplicate or invalid manifest SKU")
    by_sku = {}
    for sku, canonical_variant_id, product_gid, variant_gid in mappings:
        if sku in by_sku:
            raise ValueError("Duplicate mapped SKU")
        UUID(canonical_variant_id)
        by_sku[sku] = (canonical_variant_id, product_gid, variant_gid)
    prices = {row["sku"]: row["price_gbp"] for row in rows}
    plan = build_gbp_price_plan(
        prices,
        [VerifiedVariant(sku, pid, vid) for sku, (_, pid, vid) in by_sku.items()],
        frozenset(skus), shop_currency="GBP",
    )
    for row in rows:
        if (str(UUID(row["canonical_variant_id"])) != row["canonical_variant_id"]
                or row["canonical_variant_id"] != by_sku[row["sku"]][0]):
            raise ValueError("Canonical variant identity mismatch")
    return tuple(Target(row.sku, by_sku[row.sku][0], row.product_gid,
                        row.variant_gid, Decimal(row.price)) for row in plan)
