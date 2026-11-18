"""Read-only empirical calibration of Online Retail II customer-linked Silver.

Historical source invoice timestamps are naive. No UTC conversion, source
currency inference, synthetic event generation, or Shopify writes occur here.
"""
from __future__ import annotations

import json
from collections import Counter
from hashlib import sha256

import pandas as pd


def calibrate_historical_behavior(
    tables: dict[str, pd.DataFrame],
    *,
    approved_stock_codes: frozenset[str],
    source_sha256: str,
) -> dict:
    """Build a deterministic, source-grounded descriptive profile.

    The approved Shopify catalog is the merchandise universe for generated
    baskets. All historical customers remain in the cohort, including those
    whose source orders contain no approved merchandise.
    """
    required = {
        "customers": {"customer_id"},
        "orders": {"order_id", "customer_id", "order_timestamp"},
        "order_items": {
            "order_id", "product_id", "source_stock_code", "quantity",
            "is_positive_sale", "is_cancellation",
        },
    }
    for name, columns in required.items():
        if name not in tables or columns - set(tables[name].columns):
            raise ValueError(f"Missing Silver calibration columns in {name}")
    if not source_sha256 or len(source_sha256) != 64 or any(
        char not in "0123456789abcdef" for char in source_sha256
    ):
        raise ValueError("Expected immutable Bronze SHA-256")
    if not approved_stock_codes or not all(
        isinstance(code, str) and code.strip() == code and code for code in approved_stock_codes
    ):
        raise ValueError("Approved stock-code population is required")

    customers = tables["customers"]
    orders = tables["orders"]
    items = tables["order_items"]
    if not customers["customer_id"].is_unique or customers["customer_id"].isna().any():
        raise ValueError("Invalid historical customer identities")
    if not orders["order_id"].is_unique or orders["order_id"].isna().any():
        raise ValueError("Invalid historical order identities")
    if orders["customer_id"].isna().any() or not orders["customer_id"].isin(
        customers["customer_id"]
    ).all():
        raise ValueError("Orders reference unknown customers")
    if not items["order_id"].isin(orders["order_id"]).all():
        raise ValueError("Order items reference unknown orders")

    timestamps = pd.to_datetime(orders["order_timestamp"], errors="coerce")
    if timestamps.isna().any() or timestamps.dt.tz is not None:
        raise ValueError("Expected valid, timezone-naive historical invoice timestamps")
    is_sale = items["is_positive_sale"].fillna(False).eq(True)
    is_approved = items["source_stock_code"].isin(approved_stock_codes)
    positive = items.loc[is_sale & is_approved].copy()
    if positive["product_id"].isna().any() or positive["quantity"].isna().any():
        raise ValueError("Positive approved merchandise requires product and quantity")
    if not positive["quantity"].gt(0).all():
        raise ValueError("Positive approved merchandise requires positive quantity")

    # One invoice is one observed basket, not one invoice line.
    basket = (
        positive.groupby("order_id", sort=False)
        .agg(lines=("product_id", "size"), units=("quantity", "sum"))
        .reset_index()
    )
    observed = orders.merge(basket, on="order_id", how="inner", validate="one_to_one")
    by_customer = observed.groupby("customer_id").size().to_dict()
    customer_counts = Counter(int(by_customer.get(cid, 0)) for cid in customers["customer_id"])
    timestamp_lookup = pd.to_datetime(observed["order_timestamp"])
    product_counts = positive.groupby("source_stock_code")["quantity"].sum()

    cancellations = items["is_cancellation"].fillna(False).eq(True)
    return {
        "schema_version": "historical-behavior-calibration-v1",
        "source_bronze_sha256": source_sha256,
        "source_timestamp_semantics": "timezone_naive_no_utc_assumption",
        "currency_semantics": "unknown_source_currency_no_monetary_calibration",
        "eligible_stock_codes": len(approved_stock_codes),
        "customers_total": len(customers),
        "orders_total": len(orders),
        "approved_positive_orders": len(observed),
        "approved_positive_lines": len(positive),
        "excluded_positive_lines": int((is_sale & ~is_approved).sum()),
        "cancelled_source_lines": int(cancellations.sum()),
        "customer_order_count_histogram": {
            str(n): int(count) for n, count in sorted(customer_counts.items())
        },
        "basket_line_count_histogram": {
            str(n): int(count) for n, count in sorted(Counter(basket["lines"]).items())
        },
        "basket_unit_count_histogram": {
            str(int(n)): int(count) for n, count in sorted(Counter(basket["units"]).items())
        },
        "monthly_order_counts": {
            str(month): int(count)
            for month, count in sorted(
                Counter(timestamp_lookup.dt.to_period("M").astype(str)).items()
            )
        },
        "weekday_order_counts": {
            str(day): int(count)
            for day, count in sorted(Counter(timestamp_lookup.dt.day_of_week).items())
        },
        "hour_order_counts": {
            str(hour): int(count)
            for hour, count in sorted(Counter(timestamp_lookup.dt.hour).items())
        },
        "approved_product_unit_weights": {
            str(code): int(units)
            for code, units in sorted(product_counts.items())
        },
        "limitations": [
            "Descriptive historical distribution, not a fitted predictive model",
            "Source invoice dates are timezone naive",
            "Source currency not recorded",
            "Anonymous customer transactions excluded from customer-linked Silver",
            "Cancellations do not prove matched refunds",
            "Only approved, positive merchandise lines calibrate synthetic baskets",
        ],
    }


def canonical_calibration_bytes(profile: dict) -> bytes:
    """Stable JSON bytes for content hashing, excluding run-time metadata."""
    return (json.dumps(profile, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n").encode("utf-8")


def calibration_sha256(profile: dict) -> str:
    return sha256(canonical_calibration_bytes(profile)).hexdigest()
