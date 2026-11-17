"""Representative historical Silver reconciliation and deterministic identities."""
from pathlib import Path

import pandas as pd
import pytest

from ecommerce.pipelines.historical_silver import transform_bronze


def source_rows():
    return pd.DataFrame([
        ["536365", "85123A", "Item", 2, "2010-12-01 08:26", 2.55, 17850., "UK", "Year 2009-2010", 10],
        ["536365", "85123A", "Item", 2, "2010-12-01 08:26", 2.55, 17850., "UK", "Year 2010-2011", 5],
        ["536365", "85123A", "Item", 2, "2010-12-01 08:26", 2.55, 17850., "UK", "Year 2010-2011", 6],
        ["536366", "POST", "Shipping", 1, "2010-12-01 08:27", 2.00, 17850., "UK", "Year 2010-2011", 7],
        ["536367", "85123A", "Item", 2, "2010-12-01 08:28", 2.55, None, "UK", "Year 2010-2011", 8],
        ["C536368", "85123A", "Item", -2, "2010-12-01 08:29", 2.55, 13047., "UK", "Year 2010-2011", 9],
    ], columns=["Invoice", "StockCode", "Description", "Quantity", "InvoiceDate", "Price", "Customer ID", "Country", "source_sheet", "source_row"])


def test_reconciles_duplicates_quarantine_and_customers():
    data = source_rows()
    tables, stats = transform_bronze(data)
    assert stats["bronze_lines"] == 6
    assert stats["cross_sheet_duplicate_lines"] == 1
    assert stats["quarantined_lines"] == 1
    assert stats["customer_linked_lines"] == 4
    assert stats["customers"] == 2
    assert stats["positive_merchandise_lines"] == 2
    assert len(tables["returns_adjustments"]) == 2
    assert len(tables["product_variants"]) == 2
    assert tables["product_variants"]["shopify_variant_gid"].isna().all()
    assert tables["quarantine"].iloc[0]["reason"] == "missing_or_invalid_customer_id"
    assert tables["customers"]["shopify_customer_gid"].isna().all()
    assert tables["orders"]["currency"].isna().all()
    assert not tables["order_items"]["order_item_id"].duplicated().any()


def test_ids_stable_across_reruns():
    data = source_rows()
    first, _ = transform_bronze(data)
    second, _ = transform_bronze(data)
    assert first["customers"]["customer_id"].tolist() == second["customers"]["customer_id"].tolist()
    assert first["order_items"]["order_item_id"].tolist() == second["order_items"]["order_item_id"].tolist()


def test_rejects_conflicting_order_customer():
    rows = source_rows()
    rows.loc[3, "Invoice"] = "536365"
    rows.loc[3, "Customer ID"] = 13047.
    with pytest.raises(ValueError, match="multiple customers"):
        transform_bronze(rows)


def test_rejects_missing_provenance():
    with pytest.raises(ValueError, match="Missing Bronze columns"):
        transform_bronze(source_rows().drop(columns=["source_row"]))
