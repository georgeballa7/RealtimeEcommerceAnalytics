"""Catalog classification checks run without AWS, Shopify, or RDS."""
from uuid import uuid4

import pandas as pd
import pytest

from ecommerce.application.use_cases.catalog_selection import select_catalog_candidates


def source():
    p1, p2, p3 = (str(uuid4()) for _ in range(3))
    v1, v2, v3 = (str(uuid4()) for _ in range(3))
    products = pd.DataFrame(
        [
            {
                "product_id": p1,
                "source_stock_code": "85123A",
                "description": "Historical merchandise",
                "item_class": "merchandise_candidate",
            },
            {
                "product_id": p2,
                "source_stock_code": "POST",
                "description": "Postage",
                "item_class": "shipping_or_fee",
            },
            {
                "product_id": p3,
                "source_stock_code": "UNKNOWN!",
                "description": "Needs review",
                "item_class": "requires_review",
            },
        ]
    )
    variants = pd.DataFrame(
        [
            {
                "product_id": p1,
                "variant_id": v1,
                "source_stock_code": "85123A",
                "sku": "85123A",
            },
            {
                "product_id": p2,
                "variant_id": v2,
                "source_stock_code": "POST",
                "sku": "POST",
            },
            {
                "product_id": p3,
                "variant_id": v3,
                "source_stock_code": "UNKNOWN!",
                "sku": "UNKNOWN!",
            },
        ]
    )
    return products, variants


def test_only_candidate_merchandise_selected():
    products, variants = source()
    result = select_catalog_candidates(products, variants)
    assert len(result.candidates) == 1
    assert result.candidates[0].sku == "85123A"
    assert result.excluded_by_class == {"requires_review": 1, "shipping_or_fee": 1}
    assert (result.product_count, result.variant_count) == (3, 3)


def test_canonical_ids_are_kept_separate():
    products, variants = source()
    result = select_catalog_candidates(products, variants)
    assert result.candidates[0].product_id != result.candidates[0].variant_id


def test_mismatched_variant_is_rejected():
    products, variants = source()
    variants.loc[0, "source_stock_code"] = "WRONG"
    with pytest.raises(ValueError, match="does not match"):
        select_catalog_candidates(products, variants)


def test_unknown_parent_rejected_even_when_excluded():
    products, variants = source()
    variants.loc[2, "product_id"] = str(uuid4())
    with pytest.raises(ValueError, match="unknown product"):
        select_catalog_candidates(products, variants)


def test_duplicate_sku_is_rejected():
    products, variants = source()
    variants.loc[1, "sku"] = variants.loc[0, "sku"]
    with pytest.raises(ValueError, match="duplicate variant SKU"):
        select_catalog_candidates(products, variants)


def test_duplicate_product_code_is_rejected():
    products, variants = source()
    products.loc[1, "source_stock_code"] = products.loc[0, "source_stock_code"]
    with pytest.raises(ValueError, match="Duplicate product stock code"):
        select_catalog_candidates(products, variants)


def test_invalid_product_uuid_rejected():
    products, variants = source()
    products.loc[0, "product_id"] = "not-a-uuid"
    variants.loc[0, "product_id"] = "not-a-uuid"
    with pytest.raises(ValueError, match="Invalid product_id"):
        select_catalog_candidates(products, variants)


def test_eligible_product_requires_description():
    products, variants = source()
    products.loc[0, "description"] = None
    with pytest.raises(ValueError, match="description"):
        select_catalog_candidates(products, variants)


def test_missing_column_rejected():
    products, variants = source()
    with pytest.raises(ValueError, match="required columns"):
        select_catalog_candidates(products.drop(columns=["item_class"]), variants)
