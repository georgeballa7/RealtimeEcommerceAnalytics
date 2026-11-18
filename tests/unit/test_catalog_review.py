"""Offline catalog review: flags only, never grants Shopify write permission."""
from decimal import Decimal
from uuid import uuid4

import pandas as pd
import pytest

from ecommerce.application.use_cases.catalog_review import catalog_review


def samples():
    codes = ["85123A", "85123B", "85123C", "POST"]
    products = pd.DataFrame([
        {
            "product_id": str(uuid4()), "source_stock_code": code,
            "description": "Item " + code,
            "item_class": "shipping_or_fee" if code == "POST" else "merchandise_candidate",
        }
        for code in codes
    ])
    variants = pd.DataFrame([
        {
            "product_id": row.product_id, "variant_id": str(uuid4()),
            "source_stock_code": row.source_stock_code, "sku": row.source_stock_code,
        }
        for row in products.itertuples(index=False)
    ])
    items = pd.DataFrame([
        {"source_stock_code": "85123A", "unit_price": Decimal("1.50"), "is_positive_sale": True},
        {"source_stock_code": "85123B", "unit_price": Decimal("0.000"), "is_positive_sale": False},
        {"source_stock_code": "85123C", "unit_price": Decimal(2), "is_positive_sale": True},
        {"source_stock_code": "POST", "unit_price": Decimal(4), "is_positive_sale": False},
    ])
    bronze = pd.DataFrame([
        {"StockCode": "85123A", "Description": "The product"},
        {"StockCode": "85123A", "Description": "THE PRODUCT "},
        {"StockCode": "85123B", "Description": "Old title"},
        {"StockCode": "85123B", "Description": "New title"},
        {"StockCode": "85123C", "Description": "Stable title"},
        {"StockCode": "POST", "Description": "Postage"},
    ])
    return {"products": products, "product_variants": variants, "order_items": items}, bronze


def test_catalog_review_flags_ambiguity_and_zero_price():
    tables, bronze = samples()
    report = catalog_review(tables, bronze)
    assert report["source_products"] == 4
    assert report["candidate_products"] == 3
    assert report["excluded_by_class"] == {"shipping_or_fee": 1}
    assert report["manual_review_required"] == 1
    assert report["without_detected_flags"] == 2
    assert report["business_approved"] == 0
    flagged = [row for row in report["review_queue"] if row["needs_manual_review"]]
    assert len(flagged) == 1
    assert flagged[0]["stock_code"] == "85123B"
    assert set(flagged[0]["review_reasons"]) == {
        "multiple_source_descriptions", "observed_zero_price",
        "no_positive_merchandise_sale",
    }
    assert report["review_reason_counts_nonexclusive"] == {
        "multiple_source_descriptions": 1,
        "no_positive_merchandise_sale": 1,
        "observed_zero_price": 1,
    }


def test_no_automatic_approval_even_for_unflagged_products():
    tables, bronze = samples()
    report = catalog_review(tables, bronze)
    assert report["business_approved"] == 0
    assert all("approved" not in row for row in report["review_queue"])


def test_missing_bronze_evidence_fails_closed():
    tables, bronze = samples()
    with pytest.raises(ValueError, match="Bronze missing"):
        catalog_review(tables, bronze.drop(columns=["Description"]))


def test_invalid_unit_price_fails_closed():
    tables, bronze = samples()
    tables["order_items"].loc[0, "unit_price"] = "invalid"
    with pytest.raises(ValueError, match="Invalid historical unit price"):
        catalog_review(tables, bronze)
