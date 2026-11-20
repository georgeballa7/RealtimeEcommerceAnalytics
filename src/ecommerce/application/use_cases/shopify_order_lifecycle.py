"""Deterministic, checkpointed lifecycle for synthetic Shopify orders.

Cancellation and refunds are separate business events. A cancellation is NEVER
interpreted as a cash refund. No Shopify mutation occurs unless explicitly
requested by the caller and a durable operation has been reserved.

The adapter accepts the project's ShopifyGraphQLClient.query(document, variables,
mutation=True) interface. The caller owns the durable S3 operation journal.
"""
from __future__ import annotations

import hashlib
import random
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import uuid5, NAMESPACE_URL

ORDER_QUERY = """query Phase5Order($id: ID!) {
  order(id: $id) {
    id createdAt cancelledAt currencyCode displayFinancialStatus
    transactions(first: 20) { id kind status gateway }
    lineItems(first: 100) { pageInfo { hasNextPage } nodes { id quantity refundableQuantity } }
    refunds { id note }
  }
}"""
CANCEL_MUTATION = """mutation Phase5Cancel($id: ID!, $reason: OrderCancelReason!) {
  orderCancel(orderId: $id, reason: $reason, restock: false,
              notifyCustomer: false, refundMethod: {originalPaymentMethodsRefund: false},
              staffNote: "Synthetic Phase 5 cancellation; no automatic refund") {
    job { id done }
    orderCancelUserErrors { code field }
  }
}"""
JOB_QUERY = """query Phase5CancellationJob($id: ID!) { job(id: $id) { id done } }"""
# Shopify 2026-07 requires the idempotent directive on refundCreate.
REFUND_MUTATION = """mutation Phase5Refund($input: RefundInput!, $key: String!) {
  refundCreate(input: $input) @idempotent(key: $key) {
    refund { id note }
    userErrors { field }
  }
}"""

class LifecycleError(RuntimeError):
    """Fail-closed business or reconciliation error."""


def stable_key(run_id: str, order_id: str, operation: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,63}", run_id):
        raise ValueError("Invalid run ID")
    if not re.fullmatch(r"gid://shopify/Order/[0-9]+", order_id):
        raise ValueError("Invalid Shopify order GID")
    if operation not in {"cancel", "refund"}:
        raise ValueError("Invalid operation")
    return str(uuid5(NAMESPACE_URL, f"phase5:{run_id}:{order_id}:{operation}"))


def cancellation_plan(run_id: str, order_id: str, created_at: datetime,
                      *, seed: int, probability_percent: int = 10,
                      max_delay_days: int = 14) -> dict:
    """Stable plan; created_at must be the real creation time, not re-run time."""
    if created_at.tzinfo is None:
        raise ValueError("Creation timestamp must be timezone-aware")
    if not 0 <= probability_percent <= 100 or not 0 <= max_delay_days <= 365:
        raise ValueError("Invalid cancellation policy")
    digest = hashlib.sha256(f"{seed}:{run_id}:{order_id}:cancel".encode()).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    selected = rng.randrange(100) < probability_percent
    delay = rng.randint(0, max_delay_days * 24 * 60)
    return {"selected": selected,
            "due_at": (created_at.astimezone(timezone.utc) + timedelta(minutes=delay)).isoformat(),
            "operation_id": stable_key(run_id, order_id, "cancel"),
            "reason": "OTHER"}

def synthetic_refund_plan(run_id: str, order_id: str, created_at: datetime, *,
                          seed: int, probability_percent: int = 5,
                          max_delay_days: int = 2) -> dict:
    """Deterministically select at most one line-item refund per synthetic order.

    This plan only selects a candidate. Shopify order eligibility and whether
    any payment transaction exists must still be checked before any mutation.
    """
    if created_at.tzinfo is None:
        raise ValueError("Creation timestamp must be timezone-aware")
    if not 0 <= probability_percent <= 100 or not 0 <= max_delay_days <= 30:
        raise ValueError("Invalid synthetic refund policy")
    digest = hashlib.sha256(f"{seed}:{run_id}:{order_id}:refund".encode()).digest()
    rng = random.Random(int.from_bytes(digest[:8], "big"))
    selected = rng.randrange(100) < probability_percent
    delay_minutes = rng.randint(0, max_delay_days * 24 * 60)
    return {"selected": selected,
            "due_at": (created_at.astimezone(timezone.utc) +
                       timedelta(minutes=delay_minutes)).isoformat(),
            "operation_id": stable_key(run_id, order_id, "refund")}


def require_nonfinancial_simulation(order: dict) -> None:
    """Block automatic Shopify refund if ANY payment transaction is recorded.

    A successful Shopify refund object by itself does not prove a cash transfer.
    For this safe test path, all existing transactions must be absent; unknown
    transaction information fails closed rather than authorizing a payment.
    """
    transactions = order.get("transactions")
    if not isinstance(transactions, list) or transactions:
        raise LifecycleError("Automatic synthetic refund requires verified transactionless order")
    if order.get("displayFinancialStatus") not in {"PENDING", "VOIDED"}:
        raise LifecycleError("Automatic synthetic refund requires unpaid/voided financial status")


@dataclass(frozen=True)
class Operation:
    run_id: str
    order_id: str
    kind: str
    requested_at: str
    line_item_id: str | None = None
    quantity: int | None = None
    synthetic_nonfinancial: bool = False

    @property
    def key(self):
        return stable_key(self.run_id, self.order_id, self.kind)

    def as_record(self):
        return {"run_id": self.run_id, "order_id": self.order_id,
                "kind": self.kind, "operation_id": self.key,
                "requested_at": self.requested_at, "line_item_id": self.line_item_id,
                "quantity": self.quantity,
                "synthetic_nonfinancial": self.synthetic_nonfinancial}


def _snapshot(gql, order_id: str) -> dict:
    order = gql.query(ORDER_QUERY, {"id": order_id}).get("order")
    if not order or order.get("id") != order_id or order.get("currencyCode") != "GBP":
        raise LifecycleError("Order missing, mismatched or not GBP")
    return order


def _require_no_graphql_errors(result: dict, key: str, error_key: str):
    response = result.get(key)
    if not isinstance(response, dict) or response.get(error_key):
        raise LifecycleError(f"Shopify {key} failed; inspect Shopify before retry")
    return response


def _refund_line(order: dict, line_id: str, qty: int) -> dict:
    if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1:
        raise LifecycleError("Refund quantity must be a positive integer")
    lines = order.get("lineItems") or {}
    if (lines.get("pageInfo") or {}).get("hasNextPage"):
        raise LifecycleError("Order contains uninspected line items")
    matches = [x for x in lines.get("nodes", []) if x.get("id") == line_id]
    if len(matches) != 1 or not isinstance(matches[0].get("refundableQuantity"), int):
        raise LifecycleError("Refund line is missing or not refundable")
    if matches[0]["refundableQuantity"] < qty:
        raise LifecycleError("Refund quantity exceeds currently refundable quantity")
    return matches[0]


def advance(gql, journal, operation: Operation, *, now: datetime,
            allow_mutations: bool = False) -> str:
    """Reconcile before state changes. Ledger supports get(key), put_new(key, value), put(key, value).

    A reserved but non-committed cancel is never blindly repeated. Refunds
    have a stable Shopify idempotency key, allowing safe replay with the same
    input and key after ambiguous responses.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    requested = datetime.fromisoformat(operation.requested_at.replace("Z", "+00:00"))
    if requested.tzinfo is None:
        raise ValueError("requested_at must have a timezone")
    if now < requested:
        return "not_due"
    record = journal.get(operation.key)
    expected = operation.as_record()
    if record is not None:
        if any(record.get(k) != v for k, v in expected.items()):
            raise LifecycleError("Operation identity collision or changed retry input")
        if record.get("state") == "complete":
            return "already_complete"
        if record.get("state") not in {"reserved", "cancel_requested", "refund_requested"}:
            raise LifecycleError("Unknown journal state")
    elif not allow_mutations:
        return "preview"
    order = _snapshot(gql, operation.order_id)
    if operation.kind == "cancel":
        if order.get("cancelledAt"):
            if record is None:
                raise LifecycleError("Order already cancelled without this operation ledger")
            journal.put(operation.key, {**record, "state": "complete"})
            return "cancelled"
        if record is not None:
            job = record.get("job_id")
            if job:
                status = gql.query(JOB_QUERY, {"id": job}).get("job")
                if not status or status.get("id") != job:
                    raise LifecycleError("Cancellation job unavailable; reconcile manually")
                return "pending" if not status.get("done") else "awaiting_shopify_state"
            raise LifecycleError("Ambiguous prior cancel request; reconcile manually")
        if not allow_mutations:
            return "preview"
        journal.put_new(operation.key, {**expected, "state": "reserved"})
        response = _require_no_graphql_errors(
            gql.query(CANCEL_MUTATION, {"id": operation.order_id, "reason": "OTHER"}, mutation=True),
            "orderCancel", "orderCancelUserErrors")
        job = (response.get("job") or {}).get("id")
        if not isinstance(job, str) or not job.startswith("gid://shopify/Job/"):
            raise LifecycleError("Cancel outcome ambiguous; manual reconciliation required")
        journal.put(operation.key, {**expected, "state": "cancel_requested", "job_id": job})
        return "pending"
    if operation.kind != "refund":
        raise LifecycleError("Unknown lifecycle operation")
    if operation.line_item_id is None or operation.quantity is None:
        raise LifecycleError("Refund requires line and quantity")
    marker = f"phase5-refund:{operation.key}"
    existing = [r for r in order.get("refunds", []) if r.get("note") == marker]
    if len(existing) > 1:
        raise LifecycleError("Multiple refunds with the same synthetic identity")
    if len(existing) == 1:
        if record is None:
            raise LifecycleError("Untracked pre-existing refund; reconcile manually")
        journal.put(operation.key, {**record, "state": "complete", "refund_id": existing[0]["id"]})
        return "refunded"
    _refund_line(order, operation.line_item_id, operation.quantity)
    if operation.synthetic_nonfinancial:
        require_nonfinancial_simulation(order)
    if not allow_mutations:
        return "preview"
    if record is None:
        journal.put_new(operation.key, {**expected, "state": "reserved"})
    else:
        # A Shopify idempotent refund may be retried with identical key/input.
        # If the Shopify idempotency window has expired, fail closed rather than
        # risk another cash movement.
        first = datetime.fromisoformat(record.get("mutation_first_attempt_at", operation.requested_at))
        if now - first > timedelta(hours=12):
            raise LifecycleError("Refund idempotency window expired; manual reconciliation")
    journal.put(operation.key, {**expected, "state": "refund_requested",
                                "mutation_first_attempt_at": (record or {}).get(
                                    "mutation_first_attempt_at", now.isoformat())})
    payload = {"orderId": operation.order_id, "currency": "GBP", "notify": False,
               "note": marker,
               "refundLineItems": [{"lineItemId": operation.line_item_id,
                                    "quantity": operation.quantity, "restockType": "NO_RESTOCK"}]}
    if operation.synthetic_nonfinancial:
        payload["transactions"] = []
    response = _require_no_graphql_errors(
        gql.query(REFUND_MUTATION, {"input": payload, "key": operation.key}, mutation=True),
        "refundCreate", "userErrors")
    refund = response.get("refund") or {}
    if not str(refund.get("id", "")).startswith("gid://shopify/Refund/"):
        raise LifecycleError("Refund response ambiguous; retry only with the same key")
    journal.put(operation.key, {**expected, "state": "complete", "refund_id": refund["id"]})
    return "refunded"
