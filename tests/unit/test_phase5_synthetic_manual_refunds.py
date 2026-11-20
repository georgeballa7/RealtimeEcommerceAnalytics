"""Safe synthetic manual-gateway refund policy contracts (no live mutations)."""
import hashlib
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from ecommerce.application.use_cases.shopify_order_lifecycle import (
    LifecycleError,
    Operation,
    advance,
    require_nonfinancial_simulation,
)
from scripts import run_phase5_lifecycle


RUN_ID = "live-20261120-afternoon"
ORDER_ID = "gid://shopify/Order/19041547288897"
LINE_ID = "gid://shopify/LineItem/111"


def synthetic_order(index=149, run_id=RUN_ID):
    tag = hashlib.sha256(f"{run_id}:{index}".encode()).hexdigest()[:16]
    return {
        "id": ORDER_ID,
        "currencyCode": "GBP",
        "createdAt": "2026-11-20T15:04:20Z",
        "cancelledAt": None,
        "displayFinancialStatus": "PAID",
        "note": f"SYNTHETIC TEST ORDER; phase5 operation {run_id}/{index}",
        "tags": ["ecommerce-synthetic", "phase5-live", f"phase5-{tag}"],
        "transactions": [{
            "kind": "SALE", "status": "SUCCESS",
            "gateway": "manual", "test": False,
        }],
        "lineItems": {"pageInfo": {"hasNextPage": False}, "nodes": [
            {"id": LINE_ID, "quantity": 1, "refundableQuantity": 1}
        ]},
        "refunds": [],
    }


def test_generated_manual_payment_is_eligible_with_exact_provenance():
    require_nonfinancial_simulation(synthetic_order(), run_id=RUN_ID)


@pytest.mark.parametrize("change", [
    lambda o: o.update(note="ordinary customer order"),
    lambda o: o.update(tags=["ecommerce-synthetic", "phase5-live"]),
    lambda o: o.update(tags=["ecommerce-synthetic", "phase5-live", "phase5-forged"]),
    lambda o: o.update(transactions=[{**o["transactions"][0], "gateway": "paypal"}]),
    lambda o: o.update(transactions=[{**o["transactions"][0], "kind": "REFUND"}]),
    lambda o: o.update(transactions=[{**o["transactions"][0], "status": "PENDING"}]),
    lambda o: o.update(displayFinancialStatus="PARTIALLY_PAID"),
    lambda o: o.update(cancelledAt="2026-11-20T20:00:00Z"),
])
def test_manual_payment_fails_closed_on_mismatched_provenance_or_payment(change):
    order = synthetic_order()
    change(order)
    with pytest.raises(LifecycleError):
        require_nonfinancial_simulation(order, run_id=RUN_ID)


def test_manual_payment_requires_explicit_originating_run_id():
    with pytest.raises(LifecycleError):
        require_nonfinancial_simulation(synthetic_order())
    with pytest.raises(LifecycleError):
        require_nonfinancial_simulation(synthetic_order(), run_id="live-20261119-afternoon")


def test_nonmanual_test_false_stays_disallowed_even_with_generated_tags():
    order = synthetic_order()
    order["transactions"][0]["gateway"] = "shopify_payments"
    with pytest.raises(LifecycleError):
        require_nonfinancial_simulation(order, run_id=RUN_ID)


def test_existing_test_gateway_and_unpaid_cases_remain_eligible():
    test_order = synthetic_order()
    test_order["transactions"] = [{"kind": "SALE", "status": "SUCCESS",
                                  "gateway": "bogus", "test": True}]
    require_nonfinancial_simulation(test_order)
    pending = synthetic_order()
    pending["transactions"] = []
    pending["displayFinancialStatus"] = "PENDING"
    require_nonfinancial_simulation(pending)


def test_scheduled_refund_selection_accepts_only_proven_generated_manual(monkeypatch):
    monkeypatch.setattr(
        run_phase5_lifecycle, "selected_for_simulation",
        lambda _run, _order, kind, **kwargs: kind == "refund",
    )
    monkeypatch.setattr(
        run_phase5_lifecycle, "cancellation_plan",
        lambda *args, **kwargs: {"selected": False},
    )
    monkeypatch.setattr(
        run_phase5_lifecycle, "synthetic_refund_plan",
        lambda *args, **kwargs: {
            "selected": True, "due_at": "2026-11-20T20:00:00+00:00"
        },
    )
    gql = Mock()
    gql.query.return_value = {"order": synthetic_order()}
    operations = list(run_phase5_lifecycle.eligible_synthetic_refunds(
        gql, [(RUN_ID, ORDER_ID)],
        now=datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc),
        policy={"seed": 20261118},
        refund_policy={"probability_percent": 5, "max_delay_days": 2},
    ))
    assert len(operations) == 1
    assert operations[0].synthetic_nonfinancial is True
    assert operations[0].line_item_id == LINE_ID
    gql.query.return_value = {
        "order": {**synthetic_order(), "tags": ["other"]}
    }
    assert list(run_phase5_lifecycle.eligible_synthetic_refunds(
        gql, [(RUN_ID, ORDER_ID)],
        now=datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc),
        policy={"seed": 20261118},
        refund_policy={"probability_percent": 5, "max_delay_days": 2},
    )) == []


def test_manual_refund_create_never_submits_a_payment_reversal():
    gql = Mock()
    calls = []
    def query(doc, variables, mutation=False):
        calls.append((doc, variables, mutation))
        if not mutation:
            return {"order": synthetic_order()}
        return {"refundCreate": {"refund": {"id": "gid://shopify/Refund/234"},
                                 "userErrors": []}}
    gql.query.side_effect = query
    journal = Mock()
    journal.get.return_value = None
    operation = Operation(
        RUN_ID, ORDER_ID, "refund", "2026-11-20T21:00:00+00:00",
        LINE_ID, 1, synthetic_nonfinancial=True
    )
    result = advance(gql, journal, operation,
                     now=datetime(2026, 11, 20, 22, 0, tzinfo=timezone.utc),
                     allow_mutations=True)
    assert result == "refunded"
    mutation = [variables for _, variables, write in calls if write]
    assert len(mutation) == 1
    payload = mutation[0]["input"]
    assert payload["transactions"] == []
    assert payload["notify"] is False
    assert payload["refundLineItems"][0]["quantity"] == 1
    assert payload["refundLineItems"][0]["restockType"] == "NO_RESTOCK"
    assert payload["note"] == f"phase5-refund:{operation.key}"
    journal.put_new.assert_called_once()
    assert journal.put.call_args.args[1]["state"] == "complete"
