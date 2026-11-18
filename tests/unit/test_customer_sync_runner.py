"""Offline regression checks for the reusable customer sync entrypoint."""
from uuid import uuid4

import pandas as pd
import pytest

from ecommerce.application.use_cases.customer_sync_runner import (
    CustomerRunOptions,
    run_customer_sync,
    select_silver_customers,
)


class API:
    def __init__(self):
        self.customers = {}
        self.writes = 0

    def lookup_customer(self, customer_id):
        return self.customers.get(customer_id)

    def upsert_customer(self, customer_id, source_customer_id):
        self.writes += 1
        gid = f"gid://shopify/Customer/{self.writes}"
        self.customers[customer_id] = gid
        return gid


class Repository:
    def __init__(self):
        self.mappings = {}

    def get(self, customer_id):
        return self.mappings.get(customer_id)

    def save_verified(self, customer, gid):
        self.mappings[customer.customer_id] = gid

    def count_verified(self):
        return len(self.mappings)


def source(n=3):
    return pd.DataFrame(
        [
            {"customer_id": str(uuid4()), "source_customer_id": str(17850 + i)}
            for i in range(n)
        ]
    )


def test_selection_is_sorted_and_source_count_is_not_hardcoded():
    frame = source(3)
    selected = select_silver_customers(frame, CustomerRunOptions(limit=10))
    assert len(selected) == 3
    assert [x.customer_id for x in selected] == sorted(frame.customer_id)


def test_selection_supports_cursor_and_limit():
    frame = source(4)
    ordered = sorted(frame.customer_id)
    selected = select_silver_customers(
        frame, CustomerRunOptions(limit=1, start_after=ordered[1])
    )
    assert [x.customer_id for x in selected] == [ordered[2]]


def test_reject_duplicate_customer_ids():
    frame = source(2)
    frame.loc[1, "customer_id"] = frame.loc[0, "customer_id"]
    with pytest.raises(ValueError, match="Duplicate"):
        select_silver_customers(frame, CustomerRunOptions(limit=2))


def test_reject_invalid_options():
    with pytest.raises(ValueError, match="non-negative"):
        CustomerRunOptions(limit=-1)
    with pytest.raises(ValueError, match="batch_size"):
        CustomerRunOptions(batch_size=0)
    with pytest.raises(ValueError, match="canonical UUID"):
        CustomerRunOptions(start_after="not-a-uuid")


def test_read_only_report_does_not_write():
    selected = select_silver_customers(source(3), CustomerRunOptions(limit=3))
    repo, api, lines = Repository(), API(), []
    report = run_customer_sync(
        selected, source_population=3,
        options=CustomerRunOptions(limit=3, batch_size=2),
        api=api, repository=repo, ledger=None, emit=lines.append,
    )
    assert report.processed == 3
    assert report.verified_new == 0
    assert api.writes == 0
    assert report.verified_in_database == 0
    assert lines[-1] == "Verified mappings in PostgreSQL: 0/3"


def test_write_then_reconcile_without_recreating():
    options = CustomerRunOptions(limit=3, batch_size=2, allow_write=True)
    selected = select_silver_customers(source(3), options)
    repo, api = Repository(), API()
    first = run_customer_sync(
        selected, source_population=3, options=options,
        api=api, repository=repo, ledger=None, emit=lambda _: None,
    )
    second = run_customer_sync(
        selected, source_population=3, options=options,
        api=api, repository=repo, ledger=None, emit=lambda _: None,
    )
    assert (first.verified_new, second.skipped_existing, api.writes) == (3, 3, 3)
    assert second.verified_in_database == 3


def test_failed_customer_stops_without_false_success_report():
    options = CustomerRunOptions(limit=2, allow_write=True)
    selected = select_silver_customers(source(2), options)

    class FailingAPI(API):
        def lookup_customer(self, customer_id):
            raise RuntimeError("Shopify unavailable")

    lines = []
    with pytest.raises(RuntimeError):
        run_customer_sync(
            selected, source_population=2, options=options,
            api=FailingAPI(), repository=Repository(),
            ledger=None, emit=lines.append,
        )
    assert not any(line.startswith("Summary:") for line in lines)


def test_reject_population_mismatch():
    with pytest.raises(ValueError, match="exceeds"):
        run_customer_sync(
            [1, 2], source_population=1, options=CustomerRunOptions(),
            api=API(), repository=Repository(), ledger=None,
        )


def test_missing_source_id_rejected_before_any_remote_calls():
    frame = source(2)
    frame.loc[0, "source_customer_id"] = None
    with pytest.raises(ValueError, match="null"):
        select_silver_customers(frame, CustomerRunOptions(limit=2))


def test_duplicate_source_id_rejected():
    frame = source(2)
    frame.loc[1, "source_customer_id"] = frame.loc[0, "source_customer_id"]
    with pytest.raises(ValueError, match="Duplicate historical"):
        select_silver_customers(frame, CustomerRunOptions(limit=2))


def test_invalid_uuid_rejected_with_controlled_error():
    frame = source(2)
    frame.loc[1, "customer_id"] = "not-a-uuid"
    with pytest.raises(ValueError, match="Invalid Silver"):
        select_silver_customers(frame, CustomerRunOptions(limit=2))


def test_invalid_historical_source_id_rejected():
    frame = source(2)
    frame.loc[0, "source_customer_id"] = "17850.0"
    with pytest.raises(ValueError, match="Invalid Silver"):
        select_silver_customers(frame, CustomerRunOptions(limit=2))


def test_structured_report_is_valid_json_without_customer_data():
    import json

    options = CustomerRunOptions(limit=2, batch_size=1, allow_write=True)
    selected = select_silver_customers(source(2), options)
    lines = []
    report = run_customer_sync(
        selected, source_population=2, options=options,
        api=API(), repository=Repository(), ledger=None, emit=lines.append,
    )
    data = json.loads(report.to_json())
    assert data["selected"] == 2
    assert data["processed"] == 2
    assert data["verified_new"] == 2
    assert data["verified_in_database"] == 2
    assert data["mode"] == "WRITE"
    assert data["run_id"]
    assert data["started_at_utc"]
    assert data["completed_at_utc"]
    assert lines[-1] == "CustomerSyncReport: " + report.to_json()
    assert "17850" not in report.to_json()
    assert "gid://shopify/Customer" not in report.to_json()
