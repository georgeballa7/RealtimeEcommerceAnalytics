"""Offline invariants for deterministic synthetic 2026 orders."""
from datetime import UTC, datetime, timedelta

import pandas as pd
import pytest

from ecommerce.application.use_cases.historical_calibration import calibrate_historical_behavior
from ecommerce.application.use_cases.synthetic_backfill import (
    SimulationPolicy,
    generate_interval,
)


def fixture():
    tables = {
        "customers": pd.DataFrame({"customer_id": ["c2", "c1", "c3"]}),
        "orders": pd.DataFrame({
            "order_id": ["o1", "o2", "o3"],
            "customer_id": ["c1", "c2", "c1"],
            "order_timestamp": pd.to_datetime([
                "2010-01-04 10:10", "2010-01-05 11:10", "2010-01-06 12:10"
            ]),
        }),
        "order_items": pd.DataFrame({
            "order_id": ["o1", "o1", "o2", "o3"],
            "source_stock_code": ["A", "B", "A", "B"],
            "product_id": ["pa", "pb", "pa", "pb"],
            "quantity": [1, 500, 2, 6],
            "is_positive_sale": [True, True, True, True],
            "is_cancellation": [False, False, False, False],
        }),
        "products": pd.DataFrame({
            "source_stock_code": ["A", "B"],
            "product_id": ["pa", "pb"],
        }),
        "product_variants": pd.DataFrame({
            "source_stock_code": ["A", "B"],
            "variant_id": ["va", "vb"],
        }),
    }
    approved = frozenset({"A", "B"})
    calibration = calibrate_historical_behavior(
        tables, approved_stock_codes=approved, source_sha256="a" * 64
    )
    # Isolate generator invariants from seasonality: tiny fixture lacks the
    # 12-month/7-weekday exposure of the verified production source.
    calibration["monthly_orders_per_exposure_day"] = {
        f"2010-{month:02d}": {"orders": 1, "exposure_days": 1}
        for month in range(1, 13)
    }
    calibration["weekday_orders_per_exposure_day"] = {
        str(day): {"orders": 1, "exposure_days": 1} for day in range(7)
    }
    return tables, calibration, approved


def generate(start, end, policy=None, tables=None):
    source, calibration, approved = fixture()
    return generate_interval(
        source if tables is None else tables,
        calibration,
        approved,
        start_utc=start,
        cutoff_utc=end,
        policy=policy or SimulationPolicy(target_orders_per_day=85),
    )


def test_repeatable_and_half_open_utc_bounds():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=3)
    orders, items = generate(start, end)
    assert orders
    assert (orders, items) == generate(start, end)
    assert len({order["order_id"] for order in orders}) == len(orders)
    assert len({line["order_item_id"] for line in items}) == len(items)
    assert {line["order_id"] for line in items} == {
        order["order_id"] for order in orders
    }
    assert all(start <= datetime.fromisoformat(o["event_time_utc"]) < end for o in orders)
    assert all(o["synthetic"] and o["currency"] == "EUR" for o in orders)
    assert all(line["source_stock_code"] in {"A", "B"} for line in items)


def test_cutoff_midday_and_contiguous_restart_preserve_exact_content():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    middle = start + timedelta(days=1, hours=12)
    end = start + timedelta(days=3, hours=14)
    whole_orders, whole_items = generate(start, end)
    first_orders, first_items = generate(start, middle)
    later_orders, later_items = generate(middle, end)
    assert first_orders + later_orders == whole_orders
    assert first_items + later_items == whole_items
    assert len({o["event_id"] for o in whole_orders}) == len(whole_orders)


def test_source_row_order_does_not_affect_generated_content():
    source, _, _ = fixture()
    reverse = {key: df.iloc[::-1].reset_index(drop=True) for key, df in source.items()}
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=2)
    assert generate(start, end, tables=source) == generate(start, end, tables=reverse)


def test_bounded_basket_quantities_and_explicit_modes():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=3)
    retail = SimulationPolicy(
        target_orders_per_day=85, retail_probability_percent=100,
        retail_units_per_line_cap=3, retail_line_cap=1,
    )
    orders, items = generate(start, end, policy=retail)
    assert all(o["basket_mode"] == "retail_bounded" for o in orders)
    assert len(items) == len(orders)
    assert all(1 <= item["quantity"] <= 3 for item in items)
    assert all(float(item["line_amount"]) > 0 for item in items)


def test_fail_closed_on_naive_cutoff_and_unapproved_catalog():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(days=1)
    with pytest.raises(ValueError, match="UTC-aware"):
        generate(start, end.replace(tzinfo=None))
    source, calibration, _approved = fixture()
    with pytest.raises(ValueError, match="catalog population mismatch"):
        generate_interval(
            source, calibration, frozenset({"A"}), start_utc=start,
            cutoff_utc=end, policy=SimulationPolicy(),
        )
    with pytest.raises(ValueError, match="Invalid explicit"):
        SimulationPolicy(target_orders_per_day=0)
