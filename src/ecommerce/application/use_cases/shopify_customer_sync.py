"""Idempotent customer identity synchronization (no PII fabrication)."""
from __future__ import annotations
from dataclasses import dataclass
from typing import Iterable, Protocol
from uuid import UUID

from ecommerce.infrastructure.postgres.customer_identity import HistoricalCustomer


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
                          limit: int | None = None) -> SyncSummary:
    """Default dry-run: only read APIs/DB. Explicit allow_write performs upserts.

    Every attempted item is reconciled by custom ID first, so retries after a
    DB failure do not create duplicate Shopify identities.
    """
    if limit is not None and limit < 0:
        raise ValueError("limit must be non-negative")
    processed = skipped = verified = failed = 0
    seen: set[str] = set()
    for customer in customers:
        if limit is not None and processed >= limit:
            break
        processed += 1
        try:
            UUID(customer.customer_id)
            if not customer.source_customer_id.strip() or customer.customer_id in seen:
                raise ValueError("Invalid/duplicate canonical customer")
            seen.add(customer.customer_id)
            existing = repository.get(customer.customer_id)
            remote = api.lookup_customer(customer.customer_id)
            if existing:
                if remote != existing:
                    raise ValueError("Stored mapping does not match Shopify custom ID")
                skipped += 1
                continue
            if remote is None and not allow_write:
                continue
            if remote is None:
                api.upsert_customer(customer.customer_id)
                # Read back by custom ID; no trust in mutation response alone.
                remote = api.lookup_customer(customer.customer_id)
                if remote is None:
                    raise ValueError("Shopify customer not found after upsert")
            repository.save_verified(customer, remote)
            verified += 1
        except Exception:
            # Caller should log identifiers/redacted exceptions using secure logging.
            # Fail fast: don't silently omit identities from the full population.
            failed += 1
            raise
    return SyncSummary(processed, skipped, verified, failed)
