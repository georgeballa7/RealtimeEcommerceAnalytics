"""Pure customer-sync selection and execution; no AWS or network bootstrapping.

The existing Shopify custom-ID, durable lease, and RDS reconciliation logic
remains in shopify_customer_sync. This module only selects validated Silver
records and manages progress summaries.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import json
from uuid import UUID, uuid4

from ecommerce.application.use_cases.shopify_customer_sync import (
    customer_batches,
    synchronize_customers,
)
from ecommerce.infrastructure.postgres.customer_identity import HistoricalCustomer


@dataclass(frozen=True)
class CustomerRunOptions:
    limit: int = 5
    batch_size: int = 100
    start_after: str | None = None
    allow_write: bool = False

    def __post_init__(self):
        if self.limit < 0:
            raise ValueError("limit must be non-negative")
        if not 1 <= self.batch_size <= 1000:
            raise ValueError("batch_size must be between 1 and 1000")
        if self.start_after is not None:
            try:
                if str(UUID(self.start_after)) != self.start_after:
                    raise ValueError("Not canonical UUID")
            except ValueError as exc:
                raise ValueError("start_after must be a canonical UUID") from exc


@dataclass(frozen=True)
class CustomerRunReport:
    source_population: int
    selected: int
    processed: int
    skipped_existing: int
    verified_new: int
    verified_in_database: int
    run_id: str = ""
    mode: str = ""
    started_at_utc: str = ""
    completed_at_utc: str = ""

    def to_json(self) -> str:
        """Payload-free execution evidence for CloudWatch."""
        return json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))


def select_silver_customers(frame, options: CustomerRunOptions):
    """Validate source records before selecting a deterministic subset.

    Population comes from the checksum-verified Silver dataset, not a literal
    customer count embedded in the worker. No fabricated Shopify IDs.
    """
    required = {"customer_id", "source_customer_id"}
    if not required.issubset(frame.columns):
        raise ValueError("Silver customers missing canonical ID columns")
    if frame.empty or frame["customer_id"].isna().any():
        raise ValueError("Silver customer population is empty or has null IDs")
    if frame["customer_id"].duplicated().any():
        raise ValueError("Duplicate canonical customer IDs in Silver")
    if frame["source_customer_id"].isna().any():
        raise ValueError("Silver customer source IDs contain null values")
    if frame["source_customer_id"].astype(str).duplicated().any():
        raise ValueError("Duplicate historical source customer IDs in Silver")
    records = []
    for row in frame.sort_values("customer_id").itertuples(index=False):
        customer_id = str(row.customer_id)
        source_id = str(row.source_customer_id)
        try:
            valid_uuid = str(UUID(customer_id)) == customer_id
        except (ValueError, TypeError, AttributeError):
            valid_uuid = False
        if not valid_uuid or not source_id.isdecimal():
            raise ValueError("Invalid Silver customer identity")
        records.append(HistoricalCustomer(customer_id, source_id))
    if options.start_after is not None:
        records = [c for c in records if c.customer_id > options.start_after]
    return records[:options.limit]


def run_customer_sync(
    selected,
    *,
    source_population: int,
    options: CustomerRunOptions,
    api,
    repository,
    ledger,
    emit: Callable[[str], None] = print,
) -> CustomerRunReport:
    """Execute in bounded progress groups, preserving per-customer durability."""
    selected = tuple(selected)
    if source_population < len(selected):
        raise ValueError("Selected population exceeds source population")
    run_id = str(uuid4())
    started_at_utc = datetime.now(UTC).isoformat()
    processed = skipped = verified = 0
    emit(
        f"Mode: {'WRITE' if options.allow_write else 'READ ONLY'}; "
        f"selected={len(selected)}; batch_size={options.batch_size}"
    )
    for batch_number, batch in enumerate(
        customer_batches(selected, options.batch_size), start=1
    ):
        result = synchronize_customers(
            batch, api, repository, allow_write=options.allow_write, ledger=ledger
        )
        processed += result.processed
        skipped += result.skipped_existing
        verified += result.verified_new
        emit(
            f"Batch {batch_number}: processed={result.processed}, "
            f"skipped={result.skipped_existing}, verified_new={result.verified_new}; "
            f"total_processed={processed}"
        )
    count = repository.count_verified()
    emit(f"Summary: processed={processed}, skipped={skipped}, verified_new={verified}")
    emit(f"Verified mappings in PostgreSQL: {count}/{source_population}")
    report = CustomerRunReport(
        source_population, len(selected), processed, skipped, verified, count,
        run_id=run_id,
        mode="WRITE" if options.allow_write else "READ_ONLY",
        started_at_utc=started_at_utc,
        completed_at_utc=datetime.now(UTC).isoformat(),
    )
    emit("CustomerSyncReport: " + report.to_json())
    return report
