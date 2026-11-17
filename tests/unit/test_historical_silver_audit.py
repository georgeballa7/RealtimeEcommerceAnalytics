"""Read-only Silver audit tests, isolated from AWS."""
from decimal import Decimal

import pandas as pd

from ecommerce.pipelines.silver.audit import audit_tables


def fixture():
    tables = {
        "customers": pd.DataFrame([{"customer_id": "c1", "shopify_customer_gid": None}]),
        "products": pd.DataFrame([{"product_id": "p1", "shopify_product_gid": None}]),
        "product_variants": pd.DataFrame([{"variant_id": "v1", "product_id": "p1", "shopify_variant_gid": None}]),
        "orders": pd.DataFrame([{"order_id": "o1", "customer_id": "c1", "order_timestamp": pd.Timestamp("2010-01-02"), "currency": None, "shopify_order_gid": None}]),
        "order_items": pd.DataFrame([{"order_item_id": "i1", "order_id": "o1", "product_id": "p1", "quantity": 2,
                                     "unit_price": Decimal("1.50"), "line_amount": Decimal("3.00"),
                                     "item_class": "merchandise_candidate", "is_positive_sale": True,
                                     "is_cancellation": False, "is_return_or_credit": False}]),
        "returns_adjustments": pd.DataFrame({"order_item_id": pd.Series([], dtype="object")}),
        "quarantine": pd.DataFrame({"source_record_key": ["s:2"]}),
        "cross_sheet_duplicates": pd.DataFrame({"source_record_key": ["s:3"]}),
    }
    manifest = {"reconciliation": {"bronze_lines": 3, "customer_linked_lines": 1,
                                  "quarantined_lines": 1, "cross_sheet_duplicate_lines": 1}}
    bronze = {"total_rows": 3, "source_sha256": "test"}
    return tables, manifest, bronze


def test_healthy_relations_with_policy_warnings():
    report = audit_tables(*fixture())
    assert report["summary"]["failures"] == 0
    assert report["summary"]["warnings"] >= 2
    assert not report["summary"]["business_ready"]


def test_broken_amount_detected():
    tables, manifest, bronze = fixture()
    tables["order_items"].loc[0, "line_amount"] = Decimal("3.01")
    result = audit_tables(tables, manifest, bronze)
    assert "decimal_arithmetic" in [x["check"] for x in result["checks"] if x["status"] == "FAIL"]


def test_failed_reconciliation_detected():
    tables, manifest, bronze = fixture()
    manifest["reconciliation"]["bronze_lines"] = 4
    result = audit_tables(tables, manifest, bronze)
    assert "bronze_reconciliation" in [x["check"] for x in result["checks"] if x["status"] == "FAIL"]


def test_orphan_order_detected():
    tables, manifest, bronze = fixture()
    tables["order_items"].loc[0, "order_id"] = "missing"
    result = audit_tables(tables, manifest, bronze)
    assert "foreign_key_items_order" in [x["check"] for x in result["checks"] if x["status"] == "FAIL"]
