"""Read-only preparation of eligible historical Shopify product/variant identities.

No Shopify calls, database writes, or inferred Shopify GIDs. Product and variant
sync must share one canonical SKU/stock-code policy and explicit classification.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True)
class CatalogCandidate:
    product_id: str
    variant_id: str
    source_stock_code: str
    sku: str
    description: str


@dataclass(frozen=True)
class CatalogSelection:
    candidates: tuple[CatalogCandidate, ...]
    product_count: int
    variant_count: int
    excluded_by_class: dict[str, int]


def _uuid(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Invalid {field}")
    try:
        valid = str(UUID(value)) == value
    except (TypeError, ValueError, AttributeError):
        valid = False
    if not valid:
        raise ValueError(f"Invalid {field}")
    return value


def select_catalog_candidates(products, variants) -> CatalogSelection:
    """Fail closed on malformed IDs or relationships; exclude unapproved classes."""
    product_columns = {
        "product_id", "source_stock_code", "description", "item_class",
    }
    variant_columns = {"product_id", "variant_id", "source_stock_code", "sku"}
    if not product_columns.issubset(products.columns):
        raise ValueError("Silver products missing required columns")
    if not variant_columns.issubset(variants.columns):
        raise ValueError("Silver variants missing required columns")
    if products.empty or variants.empty:
        raise ValueError("Empty Silver catalog")
    for name, frame, key in (
        ("product", products, "product_id"),
        ("variant", variants, "variant_id"),
    ):
        if frame[key].isna().any() or frame[key].duplicated().any():
            raise ValueError(f"Invalid or duplicate {name} canonical IDs")
    if products["source_stock_code"].isna().any():
        raise ValueError("Null product stock code")
    if products["source_stock_code"].astype(str).duplicated().any():
        raise ValueError("Duplicate product stock code")
    if variants["sku"].isna().any() or variants["sku"].astype(str).duplicated().any():
        raise ValueError("Invalid or duplicate variant SKU")
    if variants["product_id"].isna().any() or (
        ~variants["product_id"].isin(products["product_id"])
    ).any():
        raise ValueError("Variant references an unknown product")

    product_by_id = {}
    exclusions = {}
    for product in products.itertuples(index=False):
        product_id = _uuid(product.product_id, "product_id")
        code = product.source_stock_code
        classification = product.item_class
        if not isinstance(code, str) or not code.strip():
            raise ValueError("Invalid historical stock code")
        if not isinstance(classification, str) or not classification:
            raise ValueError("Missing product classification")
        if classification != "merchandise_candidate":
            exclusions[classification] = exclusions.get(classification, 0) + 1
        product_by_id[product_id] = product

    candidates = []
    for variant in variants.itertuples(index=False):
        variant_id = _uuid(variant.variant_id, "variant_id")
        product_id = _uuid(variant.product_id, "variant.product_id")
        product = product_by_id[product_id]
        if product.item_class != "merchandise_candidate":
            continue
        if not isinstance(variant.source_stock_code, str) or (
            variant.source_stock_code != product.source_stock_code
        ):
            raise ValueError("Variant stock code does not match product")
        if not isinstance(variant.sku, str) or variant.sku != variant.source_stock_code:
            raise ValueError("Variant SKU does not match source stock code")
        description = product.description
        if not isinstance(description, str) or not description.strip():
            raise ValueError("Missing merchandise description")
        candidates.append(
            CatalogCandidate(
                product_id, variant_id, product.source_stock_code,
                variant.sku, description.strip(),
            )
        )
    candidates.sort(key=lambda item: (item.source_stock_code, item.variant_id))
    if len({c.product_id for c in candidates}) != len(candidates):
        raise ValueError("Expected one eligible variant per historical product")
    return CatalogSelection(
        tuple(candidates), len(products), len(variants), dict(sorted(exclusions.items()))
    )
