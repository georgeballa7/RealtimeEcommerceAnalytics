"""Regression checks for simulated refunds with no real payment movement."""
from datetime import datetime, timedelta, timezone

import pytest

from ecommerce.application.use_cases.shopify_order_lifecycle import (
    LifecycleError, Operation, advance, stable_key, synthetic_refund_plan,
)

NOW = datetime(2026, 11, 20, 12, tzinfo=timezone.utc)
ORDER = "gid://shopify/Order/123"
LINE = "gid://shopify/LineItem/456"


class Journal:
    def __init__(self):
        self.data = {}

    def get(self, key):
        return self.data.get(key)

    def put_new(self, key, record):
        assert key not in self.data
        self.data[key] = dict(record)

    def put(self, key, record):
        assert key in self.data
        self.data[key] = dict(record)


class Shopify:
    def __init__(self, transactions=None, status="PENDING", available=1):
        self.transactions = transactions if transactions is not None else []
        self.status = status
        self.available = available
        self.mutations = []

    def query(self, document, variables, mutation=False):
        if mutation:
            self.mutations.append(variables)
            return {"refundCreate": {"refund": {"id": "gid://shopify/Refund/999"}, "userErrors": []}}
        return {"order": {
            "id": ORDER, "currencyCode": "GBP", "displayFinancialStatus": self.status,
            "cancelledAt": None, "transactions": self.transactions,
            "refunds": [], "lineItems": {"pageInfo": {"hasNextPage": False},
                                        "nodes": [{"id": LINE, "refundableQuantity": self.available}]},
        }}


def operation():
    return Operation("run001", ORDER, "refund", NOW.isoformat(), LINE, 1,
                     synthetic_nonfinancial=True)


def test_deterministic_plan_and_bounds():
    plan = synthetic_refund_plan("run001", ORDER, NOW, seed=20261118,
                                 probability_percent=100)
    assert plan == synthetic_refund_plan("run001", ORDER, NOW, seed=20261118,
                                         probability_percent=100)
    assert plan["selected"]
    assert NOW <= datetime.fromisoformat(plan["due_at"]) <= NOW + timedelta(days=2)
    assert plan["operation_id"] == stable_key("run001", ORDER, "refund")


@pytest.mark.parametrize("transactions,status", [
    ([{"id": "gid://shopify/OrderTransaction/1", "status": "SUCCESS"}], "PAID"),
    ([{"id": "gid://shopify/OrderTransaction/1", "status": "PENDING"}], "PENDING"),
    ([], "PAID"),
])
def test_no_real_payment_refund(transactions, status):
    client, journal = Shopify(transactions, status), Journal()
    with pytest.raises(LifecycleError, match="synthetic refund"):
        advance(client, journal, operation(), now=NOW, allow_mutations=True)
    assert not client.mutations
    assert not journal.data


def test_transactionless_refund_is_explicitly_nonfinancial_and_idempotent():
    client, journal = Shopify(), Journal()
    assert advance(client, journal, operation(), now=NOW, allow_mutations=False) == "preview"
    assert not client.mutations
    assert advance(client, journal, operation(), now=NOW, allow_mutations=True) == "refunded"
    assert client.mutations[0]["input"]["transactions"] == []
    assert client.mutations[0]["key"] == operation().key
    assert advance(client, journal, operation(), now=NOW, allow_mutations=True) == "already_complete"
    assert len(client.mutations) == 1


def test_nonfinancial_and_explicit_refund_journals_are_distinct_contracts():
    explicit = Operation("run001", ORDER, "refund", NOW.isoformat(), LINE, 1)
    assert "synthetic_nonfinancial" not in explicit.as_record()
    assert operation().as_record()["synthetic_nonfinancial"] is True
