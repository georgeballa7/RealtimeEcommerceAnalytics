"""Reconcile canonical Shopify customers with optional durable ledger.

No cross-system atomic transaction is implied: read-back by custom ID protects
retries following an ambiguous Shopify response or interrupted database commit.
"""
from __future__ import annotations
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Iterable, Protocol
from uuid import UUID

from ecommerce.infrastructure.postgres.customer_identity import HistoricalCustomer
from ecommerce.infrastructure.postgres.customer_sync_ledger import AttemptEvent


class CustomerAPI(Protocol):
    def lookup_customer(self, canonical_id: str) -> str | None: ...
    def upsert_customer(self, canonical_id: str) -> str: ...


class MappingRepository(Protocol):
    def get(self, customer_id: str) -> str | None: ...
    def save_verified(self, customer: HistoricalCustomer, gid: str) -> None: ...


@dataclass(frozen=True)
class SyncSummary:
    processed: int
    skipped_existing: int
    verified_new: int
    failures: int


def synchronize_customers(customers: Iterable[HistoricalCustomer], api: CustomerAPI,
                          repository: MappingRepository, *, allow_write: bool = False,
                          limit: int | None = None, ledger=None) -> SyncSummary:
    """Read-only by default; a failing identity stops the batch immediately.

    When provided, ledger.claim holds a bounded lease for the whole identity,
    and ledger.record commits events before/after the corresponding remote work.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    processed = skipped = verified = failed = 0
    seen: set[str] = set()
    for customer in customers:
        if limit is not None and processed >= limit:
            break
        processed += 1
        UUID(customer.customer_id)
        if not customer.source_customer_id.strip() or customer.customer_id in seen:
            raise ValueError("Invalid/duplicate canonical customer")
        seen.add(customer.customer_id)
        context = ledger.claim(customer.customer_id) if ledger else nullcontext()
        with context:
            def record(phase: str, outcome: str, gid: str | None = None,
                       code: str | None = None) -> None:
                if ledger:
                    ledger.record(customer.customer_id, AttemptEvent(phase, outcome, gid, code))

            stage = "lookup"
            try:
                existing = repository.get(customer.customer_id)
                record("lookup", "started")
                remote = api.lookup_customer(customer.customer_id)
                record("lookup", "verified" if remote else "not_found", remote)
                if existing:
                    if remote != existing:
                        raise ValueError("Stored mapping does not match Shopify custom ID")
                    skipped += 1
                    continue
                if remote is None and not allow_write:
                    continue
                if remote is None:
                    stage = "upsert"
                    record("upsert", "started")  # durable BEFORE remote mutation
                    api.upsert_customer(customer.customer_id)
                    stage = "reconcile"
                    record("reconcile", "started")
                    remote = api.lookup_customer(customer.customer_id)
                    if remote is None:
                        raise ValueError("Shopify customer not found after upsert")
                    record("reconcile", "verified", remote)
                stage = "persist"
                record("persist", "started", remote)
                repository.save_verified(customer, remote)
                record("persist", "verified", remote)
                verified += 1
            except Exception:
                failed += 1
                # Never store tokens, customer PII, API payloads or exception text.
                # On the next invocation lookup precedes every new mutation.
                record(stage, "ambiguous" if stage in {"upsert", "reconcile"} else "failed",
                       code="RECONCILE_REQUIRED" if stage in {"upsert", "reconcile"}
                       else "SYNC_FAILED")
                raise
    return SyncSummary(processed, skipped, verified, failed)
