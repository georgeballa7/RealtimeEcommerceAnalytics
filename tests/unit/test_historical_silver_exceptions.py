"""Offline business-rule exception analysis tests."""
from decimal import Decimal

import pandas as pd
import pytest

from ecommerce.pipelines.silver.exceptions import analyze_exceptions


def tables():
    orders = pd.DataFrame([
        {"order_id": "o1", "customer_id": "c1"},
        {"order_id": "o2", "customer_id": "c1"},
        {"order_id": "o3", "customer_id": "c2"},
    ])
    def item(identity, order, date, quantity, price, *, cancellation=False, code="12345", klass="merchandise_candidate"):
        return {"order_item_id": identity, "source_record_key": f"s:{identity}",
                "order_id": order, "source_stock_code": code, "quantity": quantity,
                "unit_price": Decimal(price), "item_class": klass,
                "is_cancellation": cancellation, "order_timestamp": pd.Timestamp(date)}
    items = pd.DataFrame([
        item("a", "o1", "2010-01-01", 2, "2.00"),
        item("b", "o1", "2010-01-02", 2, "2.00"),
        item("c", "o2", "2010-01-03", -2, "2.00", cancellation=True),
        item("d", "o2", "2010-01-03", -3, "2.00", cancellation=True),
        item("e", "o3", "2010-01-03", -2, "2.00", cancellation=True),
        item("f", "o2", "2010-01-04", 1, "0", code="TEST", klass="requires_review"),
    ])
    products = pd.DataFrame([{"source_stock_code": "12345", "description": "Product"},
                             {"source_stock_code": "TEST", "description": "Review"}])
    return {"order_items": items, "orders": orders, "products": products}


def test_cancellation_candidates_not_confirmed():
    report = analyze_exceptions(tables())
    assert report["cancellations"]["matching_patterns"] == {
        "multiple_exact_candidates": 1, "no_exact_candidate": 2}
    assert report["cancellations"]["negative_quantity_lines"] == 3
    assert report["cancellations"]["samples"][0]["candidate_count"] == 2


def test_zero_price_and_code_review():
    report = analyze_exceptions(tables())
    assert report["zero_price"]["lines"] == 1
    assert report["ambiguous_product_codes"]["requires_review_codes"] == [
        {"stock_code": "TEST", "lines": 1}]
    assert report["ambiguous_product_codes"]["bronze_analyzed"] is False


def test_sample_cap_and_invalid_limit():
    report = analyze_exceptions(tables(), sample_limit=1)
    assert len(report["cancellations"]["samples"]) == 1
    with pytest.raises(ValueError):
        analyze_exceptions(tables(), sample_limit=-1)


def test_bronze_description_ambiguity():
    bronze = pd.DataFrame([
        {"StockCode": "12345", "Description": "Blue mug"},
        {"StockCode": "12345", "Description": "BLUE MUG "},
        {"StockCode": "12345", "Description": "Red mug"},
        {"StockCode": "TEST", "Description": "Other"},
    ])
    report = analyze_exceptions(tables(), bronze=bronze)
    assert report["ambiguous_product_codes"]["codes_with_multiple_descriptions"] == 1
    assert report["ambiguous_product_codes"]["description_ambiguities"][0]["distinct_descriptions"] == 2
