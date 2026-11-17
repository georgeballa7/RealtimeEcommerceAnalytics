"""Offline read-only Silver business review tests."""
from decimal import Decimal

import pandas as pd
import pytest

from ecommerce.pipelines.silver.review_v2 import review_business_rules


def fixture():
    orders = pd.DataFrame([
        {"order_id": "s1", "customer_id": "c1"},
        {"order_id": "s2", "customer_id": "c1"},
        {"order_id": "s3", "customer_id": "c2"},
    ])
    def row(id_, order, time, qty, price, code="12345", cancel=False):
        return {"order_item_id": id_, "source_record_key": id_, "order_id": order,
                "source_stock_code": code, "order_timestamp": pd.Timestamp(time),
                "quantity": qty, "unit_price": Decimal(price),
                "is_cancellation": cancel, "item_class": "merchandise_candidate"}
    items = pd.DataFrame([
        row("a", "s1", "2010-01-01", 5, "3"),
        row("b", "s1", "2010-01-02", 3, "3"),
        row("c", "s2", "2010-01-03", -2, "3", cancel=True),
        row("d", "s2", "2010-01-04", -6, "3", cancel=True),
        row("e", "s2", "2010-01-05", -1, "3", cancel=True),
        row("f", "s3", "2010-01-05", -2, "3", cancel=True),
        row("g", "s1", "2010-01-05", 1, "0"),
    ])
    bronze = pd.DataFrame([
        {"StockCode": "12345", "Description": "Blue mug", "Quantity": 5, "Price": 3, "Invoice": "123"},
        {"StockCode": "12345", "Description": "BLUE MUG ", "Quantity": 3, "Price": 3, "Invoice": "124"},
        {"StockCode": "12345", "Description": "DAMAGED", "Quantity": -1, "Price": 3, "Invoice": "C125"},
        {"StockCode": "12345", "Description": "Red mug", "Quantity": 1, "Price": 3, "Invoice": "126"},
    ])
    return {"orders": orders, "order_items": items}, bronze


def test_fifo_partial_multi_and_capacity_exhaustion():
    tables, bronze = fixture()
    report = review_business_rules(tables, bronze)
    simulation = report["cancellation_allocation_simulation"]
    assert simulation["patterns"] == {"covered_by_one_purchase": 1,
                                       "covered_by_multiple_purchases": 1,
                                       "no_prior_paid_capacity": 2}
    assert simulation["cancelled_units"] == 11
    assert simulation["simulated_allocated_units"] == 8
    assert simulation["unallocated_units"] == 3


def test_description_suggestion_ignores_operational_note():
    tables, bronze = fixture()
    report = review_business_rules(tables, bronze)
    assert report["product_descriptions"]["codes_with_multiple_descriptions"] == 1
    assert report["product_descriptions"]["examples"][0]["suggested_description"] == "BLUE MUG"
    assert report["product_descriptions"]["operational_note_lines_heuristic"] == 1
    assert report["zero_price"]["lines"] == 1


def test_same_timestamp_sale_not_eligible():
    tables, bronze = fixture()
    tables["order_items"].loc[tables["order_items"]["source_record_key"].eq("a"), "order_timestamp"] = pd.Timestamp("2010-01-03")
    tables["order_items"] = tables["order_items"].loc[~tables["order_items"]["source_record_key"].isin(["b", "d", "e", "f", "g"])].copy()
    report = review_business_rules(tables, bronze)
    assert report["cancellation_allocation_simulation"]["patterns"] == {"no_prior_paid_capacity": 1}


def test_bad_sample_limit_rejected():
    tables, bronze = fixture()
    with pytest.raises(ValueError):
        review_business_rules(tables, bronze, sample_limit=-1)


def test_partially_coverable_return_and_cross_customer_isolation():
    tables, bronze = fixture()
    # Only one earlier sale of two units belongs to c1. A c2 purchase must
    # not be used for c1, and only two of a later three-unit return can fit.
    tables["order_items"] = pd.DataFrame([
        {"order_item_id": "p", "source_record_key": "p", "order_id": "s1",
         "source_stock_code": "12345", "order_timestamp": pd.Timestamp("2010-01-01"),
         "quantity": 2, "unit_price": Decimal("3"), "is_cancellation": False,
         "item_class": "merchandise_candidate"},
        {"order_item_id": "q", "source_record_key": "q", "order_id": "s3",
         "source_stock_code": "12345", "order_timestamp": pd.Timestamp("2010-01-01"),
         "quantity": 10, "unit_price": Decimal("3"), "is_cancellation": False,
         "item_class": "merchandise_candidate"},
        {"order_item_id": "r", "source_record_key": "r", "order_id": "s2",
         "source_stock_code": "12345", "order_timestamp": pd.Timestamp("2010-01-02"),
         "quantity": -3, "unit_price": Decimal("3"), "is_cancellation": True,
         "item_class": "merchandise_candidate"},
    ])
    simulation = review_business_rules(tables, bronze)["cancellation_allocation_simulation"]
    assert simulation["patterns"] == {"partial_capacity": 1}
    assert simulation["simulated_allocated_units"] == 2
    assert simulation["unallocated_units"] == 1


def test_input_dataframes_are_not_modified():
    tables, bronze = fixture()
    before_items = tables["order_items"].copy(deep=True)
    before_bronze = bronze.copy(deep=True)
    review_business_rules(tables, bronze, sample_limit=0)
    pd.testing.assert_frame_equal(tables["order_items"], before_items)
    pd.testing.assert_frame_equal(bronze, before_bronze)
