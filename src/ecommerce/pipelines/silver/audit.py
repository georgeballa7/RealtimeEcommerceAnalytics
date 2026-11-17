"""Read-only historical Silver integrity and business-rule audit."""
from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import io
import json
from typing import Any

import pandas as pd

EXPECTED_TABLES = ("customers", "products", "product_variants", "orders", "order_items",
                   "returns_adjustments", "quarantine", "cross_sheet_duplicates")


def _download(s3: Any, bucket: str, key: str) -> bytes:
    stream = s3.get_object(Bucket=bucket, Key=key)["Body"]
    try:
        return stream.read()
    finally:
        stream.close()


def read_published_silver(s3: Any, bucket: str, prefix: str):
    """Read exactly the published S3 dataset, verifying checksums and row counts."""
    prefix = prefix.strip("/")
    silver = json.loads(_download(s3, bucket, f"{prefix}/manifest.json"))
    if silver.get("schema_version") != "online-retail-ii-silver-v1":
        raise ValueError("Unexpected Silver schema")
    files = silver.get("files", [])
    names = [x["key"].removeprefix(prefix + "/").split("/")[0] for x in files]
    if sorted(names) != sorted(EXPECTED_TABLES):
        raise ValueError(f"Unexpected Silver table set: {names}")
    tables = {}
    for entry, name in zip(files, names):
        key = entry["key"]
        if not key.startswith(prefix + "/") or not key.endswith(".parquet"):
            raise ValueError(f"Unexpected Silver key: {key}")
        raw = _download(s3, bucket, key)
        if sha256(raw).hexdigest() != entry["sha256"]:
            raise ValueError(f"Silver checksum mismatch: {key}")
        frame = pd.read_parquet(io.BytesIO(raw), engine="pyarrow")
        if len(frame) != entry["rows"]:
            raise ValueError(f"Silver row-count mismatch: {key}")
        tables[name] = frame
    bronze = json.loads(_download(s3, bucket, silver["source_bronze_manifest_key"]))
    if bronze.get("source_sha256") != silver.get("source_bronze_sha256"):
        raise ValueError("Silver/Bronze lineage mismatch")
    return tables, silver, bronze


def audit_tables(tables: dict[str, pd.DataFrame], silver: dict, bronze: dict) -> dict:
    """Report PASS, WARN and FAIL without changing any source records."""
    findings = []

    def check(name, status, detail, **metrics):
        findings.append({"check": name, "status": status, "detail": detail, "metrics": metrics})

    missing = set(EXPECTED_TABLES) - set(tables)
    if missing:
        raise ValueError(f"Missing Silver tables: {sorted(missing)}")
    items = tables["order_items"]
    customers, orders = tables["customers"], tables["orders"]
    products, variants = tables["products"], tables["product_variants"]
    quarantine, duplicates = tables["quarantine"], tables["cross_sheet_duplicates"]
    totals = silver.get("reconciliation", {})
    original = bronze.get("total_rows")
    passes = (len(items) + len(quarantine) + len(duplicates) == original == totals.get("bronze_lines")
              and len(items) == totals.get("customer_linked_lines")
              and len(quarantine) == totals.get("quarantined_lines")
              and len(duplicates) == totals.get("cross_sheet_duplicate_lines"))
    check("bronze_reconciliation", "PASS" if passes else "FAIL",
          "Every Bronze line must reconcile exactly.", source=original, linked=len(items),
          quarantined=len(quarantine), duplicate_copies=len(duplicates))

    customer_ok = original != 1_067_371 or len(customers) == 5_942
    check("customer_population", "PASS" if customer_ok else "FAIL",
          "All 5,942 identifiable customers must be present in the full historical dataset.",
          actual=len(customers), expected=5_942 if original == 1_067_371 else None)

    for table, pk in (("customers", "customer_id"), ("products", "product_id"),
                      ("product_variants", "variant_id"), ("orders", "order_id"),
                      ("order_items", "order_item_id")):
        frame = tables[table]
        valid = pk in frame and frame[pk].notna().all() and frame[pk].is_unique
        check(f"primary_key_{table}", "PASS" if valid else "FAIL", "Unique, non-null primary keys.",
              rows=len(frame))

    for name, child, fk, parent, pk in (
        ("orders_customer", orders, "customer_id", customers, "customer_id"),
        ("items_order", items, "order_id", orders, "order_id"),
        ("items_product", items, "product_id", products, "product_id"),
        ("variants_product", variants, "product_id", products, "product_id"),
    ):
        bad = int((child[fk].notna() & ~child[fk].isin(parent[pk])).sum())
        check(f"foreign_key_{name}", "FAIL" if bad else "PASS",
              "Non-null foreign keys must resolve.", orphans=bad)

    shopify = sum(int(tables[t][field].notna().sum()) for t, field in (
        ("customers", "shopify_customer_gid"), ("products", "shopify_product_gid"),
        ("product_variants", "shopify_variant_gid"), ("orders", "shopify_order_gid")))
    check("shopify_identity_not_assumed", "PASS" if shopify == 0 else "WARN",
          "Non-null Shopify identifiers require independent sync evidence.", populated=shopify)

    actual_adjustments = set(tables["returns_adjustments"]["order_item_id"].astype(str))
    expected_adjustments = set(items.loc[~items["is_positive_sale"], "order_item_id"].astype(str))
    check("adjustment_partition", "PASS" if actual_adjustments == expected_adjustments else "FAIL",
          "All non-positive-sales lines must be in adjustments.",
          actual=len(actual_adjustments), expected=len(expected_adjustments))

    invalid_sales = int((items["is_positive_sale"].fillna(False)
                         & (items["is_cancellation"].fillna(False) | items["quantity"].le(0))).sum())
    check("positive_sale_exclusions", "PASS" if invalid_sales == 0 else "FAIL",
          "Cancelled and nonpositive-quantity lines cannot be positive sales.", invalid=invalid_sales)

    money_bad, nond_decimal_price, nond_decimal_amount, missing_money = 0, 0, 0, 0
    for quantity, price, amount in zip(items["quantity"], items["unit_price"], items["line_amount"]):
        if pd.isna(quantity) or pd.isna(price) or pd.isna(amount):
            missing_money += 1
            continue
        nond_decimal_price += not isinstance(price, Decimal)
        nond_decimal_amount += not isinstance(amount, Decimal)
        try:
            if Decimal(str(int(quantity))) * Decimal(str(price)) != Decimal(str(amount)):
                money_bad += 1
        except (InvalidOperation, ValueError, TypeError):
            money_bad += 1
    good_money = not (money_bad or nond_decimal_price or nond_decimal_amount or missing_money)
    check("decimal_arithmetic", "PASS" if good_money else "FAIL",
          "Financial values must be exact Decimal and equal quantity × unit price.",
          mismatches=money_bad, non_decimal_prices=nond_decimal_price,
          non_decimal_amounts=nond_decimal_amount, missing=missing_money)

    classes = {str(k): int(v) for k, v in items["item_class"].fillna("<null>").value_counts().items()}
    check("classification_review", "WARN",
          "Product, shipping, discount and ambiguous codes need business review.", classes=classes,
          positive_merchandise=int(items["is_positive_sale"].fillna(False).sum()))
    missing_currency = int(orders["currency"].isna().sum())
    check("currency_provenance", "WARN" if missing_currency else "PASS",
          "Source has no currency field; never assume GBP without evidence.",
          missing_currency=missing_currency)
    negatives = int(items["unit_price"].map(lambda x: pd.notna(x) and Decimal(str(x)) < 0).sum())
    zeroes = int(items["unit_price"].map(lambda x: pd.notna(x) and Decimal(str(x)) == 0).sum())
    check("nonpositive_price_review", "WARN" if negatives or zeroes else "PASS",
          "Investigate negative/zero prices.", negative=negatives, zero=zeroes)
    cancellations = int(items["is_cancellation"].fillna(False).sum())
    negative_qty = int(items["quantity"].lt(0).sum())
    check("returns_refunds_review", "WARN" if cancellations or negative_qty else "PASS",
          "Cancellation flags do not prove matched refunds.", cancellations=cancellations,
          negative_quantities=negative_qty)
    missing_dates = int(orders["order_timestamp"].isna().sum())
    check("order_dates", "FAIL" if missing_dates else "PASS",
          "Preserve original naive invoice date; do not infer UTC.", missing=missing_dates)
    check("anonymous_revenue_limitation", "WARN" if len(quarantine) else "PASS",
          "Excluding anonymous lines prevents all-revenue interpretation.",
          excluded=len(quarantine))

    counts = Counter(x["status"] for x in findings)
    return {"audit_version": "historical-silver-business-audit-v1",
            "generated_at_utc": datetime.now(UTC).isoformat(),
            "bronze_source_sha256": bronze.get("source_sha256"),
            "summary": {"passes": counts["PASS"], "warnings": counts["WARN"],
                        "failures": counts["FAIL"], "business_ready": counts["WARN"] == counts["FAIL"] == 0},
            "checks": findings}


def audit_published_silver(s3: Any, bucket: str, prefix: str) -> dict:
    tables, silver, bronze = read_published_silver(s3, bucket, prefix)
    return audit_tables(tables, silver, bronze)
