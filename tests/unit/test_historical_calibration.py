"""Source-faithful, deterministic Phase 4 calibration tests (offline only)."""
from __future__ import annotations

import pandas as pd
import pytest

from ecommerce.application.use_cases.historical_calibration import (
    calibrate_historical_behavior,
    calibration_sha256,
    canonical_calibration_bytes,
)


def silver():
    return {
        "customers": pd.DataFrame({"customer_id": ["c1", "c2", "c3"]}),
        "orders": pd.DataFrame({
            "order_id": ["o1", "o2", "o3"],
            "customer_id": ["c1", "c1", "c2"],
            "order_timestamp": pd.to_datetime([
                "2010-01-05 09:10", "2010-02-08 15:20", "2010-02-09 09:00"
            ]),
        }),
        "order_items": pd.DataFrame({
            "order_id": ["o1", "o1", "o2", "o3", "o3"],
            "product_id": ["p1", "p1", "p2", "p1", "p2"],
            "source_stock_code": ["A", "A", "B", "A", "B"],
            "quantity": [2, 1, 5, -1, 2],
            "is_positive_sale": [True, True, True, False, True],
            "is_cancellation": [False, False, False, True, False],
        }),
    }


def run(tables=None):
    return calibrate_historical_behavior(
        silver() if tables is None else tables,
        approved_stock_codes=frozenset({"A", "B"}),
        source_sha256="a" * 64,
    )


def test_uses_invoice_basket_grain_and_includes_all_customers():
    report = run()
    assert report["customers_total"] == 3
    assert report["orders_total"] == 3
    assert report["approved_positive_orders"] == 3
    assert report["approved_positive_lines"] == 4
    assert report["cancelled_source_lines"] == 1
    assert report["schema_version"] == "historical-behavior-calibration-v2"
    assert report["source_orders_with_cancellation_lines"] == 1
    assert report["source_orders_without_cancellation_lines"] == 2
    assert report["approved_positive_orders_with_cancellation_lines"] == 1
    assert report["customer_order_count_percentiles"] == {
        "p50": 1, "p90": 2, "p95": 2, "p99": 2
    }
    assert report["basket_line_count_percentiles"] == {
        "p50": 1, "p90": 2, "p95": 2, "p99": 2
    }
    assert report["basket_unit_count_percentiles"] == {
        "p50": 3, "p90": 5, "p95": 5, "p99": 5
    }
    assert report["observation_start_source_date"] == "2010-01-05"
    assert report["observation_end_source_date_inclusive"] == "2010-02-09"
    assert report["observation_calendar_days"] == 36
    assert report["month_exposure_days"] == {"2010-01": 27, "2010-02": 9}
    assert report["monthly_orders_per_exposure_day"]["2010-01"] == {
        "orders": 1, "exposure_days": 27
    }
    assert report["weekday_orders_per_exposure_day"]["0"]["orders"] == 1
    assert report["weekday_orders_per_exposure_day"]["0"]["exposure_days"] == 5
    assert report["customer_order_count_histogram"] == {"0": 1, "1": 1, "2": 1}
    assert report["basket_line_count_histogram"] == {"1": 2, "2": 1}
    assert report["basket_unit_count_histogram"] == {"2": 1, "3": 1, "5": 1}
    assert report["monthly_order_counts"] == {"2010-01": 1, "2010-02": 2}
    assert report["approved_product_unit_weights"] == {"A": 3, "B": 7}
    assert report["currency_semantics"].startswith("unknown_source_currency")


def test_filters_products_to_pinned_approved_population():
    data = silver()
    report = calibrate_historical_behavior(
        data, approved_stock_codes=frozenset({"A"}), source_sha256="a" * 64
    )
    assert report["approved_positive_orders"] == 1
    assert report["excluded_positive_lines"] == 2
    assert report["customer_order_count_histogram"] == {"0": 2, "1": 1}


def test_deterministic_bytes_independent_of_input_row_order():
    first = run()
    data = silver()
    for name in data:
        data[name] = data[name].iloc[::-1].reset_index(drop=True)
    second = run(data)
    assert canonical_calibration_bytes(first) == canonical_calibration_bytes(second)
    assert calibration_sha256(first) == calibration_sha256(second)


def test_fails_closed_on_invalid_identity_or_timezone():
    data = silver()
    data["orders"].loc[0, "customer_id"] = "unknown"
    with pytest.raises(ValueError, match="unknown customers"):
        run(data)
    data = silver()
    data["orders"]["order_timestamp"] = data["orders"]["order_timestamp"].dt.tz_localize(
        "UTC"
    )
    with pytest.raises(ValueError, match="timezone-naive"):
        run(data)


def test_fails_closed_on_missing_approval_and_invalid_positive_quantity():
    with pytest.raises(ValueError, match="Approved stock-code"):
        calibrate_historical_behavior(silver(), approved_stock_codes=frozenset(),
                                      source_sha256="a" * 64)
    data = silver()
    data["order_items"].loc[0, "quantity"] = 0
    with pytest.raises(ValueError, match="positive quantity"):
        run(data)

def test_no_eligible_orders_fails_with_clear_statistical_precondition():
    with pytest.raises(ValueError, match="empty population"):
        calibrate_historical_behavior(
            silver(), approved_stock_codes=frozenset({"UNUSED"}), source_sha256="a" * 64
        )


def test_calendar_exposure_uses_all_source_orders_not_only_approved():
    report = calibrate_historical_behavior(
        silver(), approved_stock_codes=frozenset({"A"}), source_sha256="a" * 64
    )
    assert report["observation_calendar_days"] == 36
    assert report["month_exposure_days"]["2010-02"] == 9
    assert report["monthly_orders_per_exposure_day"]["2010-02"] == {
        "orders": 0, "exposure_days": 9
    }
