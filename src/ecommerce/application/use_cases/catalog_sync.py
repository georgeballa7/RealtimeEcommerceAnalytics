"""Restart-safe catalog identity reconciliation with explicit write authorization.

Shopify and PostgreSQL cannot commit atomically. Always find the remote product
by a unique canonical product custom ID and find its variant by product+SKU
before any create. Re-read after every mutation; persist only verified GIDs.
The caller must hold an exclusive durable per-product lease for write mode.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, fields
from typing import Protocol

from ecommerce.application.use_cases.catalog_selection import CatalogCandidate


class CatalogAPI(Protocol):
    def lookup_product(self, product_id: str) -> str | None: ...
    def create_product(self, candidate: CatalogCandidate) -> str: ...
    def lookup_variant(self, product_gid: str, sku: str) -> str | None: ...
    def create_variant(self, product_gid: str, candidate: CatalogCandidate) -> str: ...


class CatalogRepository(Protocol):
    def get_product(self, product_id: str) -> str | None: ...
    def get_variant(self, variant_id: str) -> tuple[str, str] | None: ...
    def save_product(self, candidate: CatalogCandidate, gid: str) -> None: ...
    def save_variant(
        self, candidate: CatalogCandidate, product_gid: str, variant_gid: str
    ) -> None: ...


@dataclass(frozen=True)
class CatalogSyncSummary:
    inspected: int
    product_mappings_verified: int
    variant_mappings_verified: int
    product_creates_requested: int
    variant_creates_requested: int
    missing_products: int
    missing_variants: int


def reconcile_catalog(
    candidates: Iterable[CatalogCandidate],
    api: CatalogAPI,
    repository: CatalogRepository,
    *,
    allow_write: bool = False,
    lease=None,
) -> CatalogSyncSummary:
    """Fail closed on identity conflicts or ambiguous Shopify mutations.

    Read-only mode never sends mutations or persists RDS identity mappings.
    For write mode a durable lease provider is mandatory, and the provider
    must be shared across concurrently running ECS workers.
    """
    if allow_write and lease is None:
        raise ValueError("Write-enabled catalog sync requires a durable lease")
    inspected = products = variants = pcreates = vcreates = missing_p = missing_v = 0
    seen = set()
    for candidate in candidates:
        if candidate.product_id in seen:
            raise ValueError("Duplicate product in catalog sync run")
        seen.add(candidate.product_id)
        inspected += 1
        if allow_write:
            context = lease.claim(candidate.product_id)
        else:
            from contextlib import nullcontext

            context = nullcontext()
        with context:
            mapped_product = repository.get_product(candidate.product_id)
            remote_product = api.lookup_product(candidate.product_id)
            if mapped_product is not None and remote_product != mapped_product:
                raise ValueError("Existing product mapping conflicts with Shopify")
            if remote_product is None:
                missing_p += 1
                if not allow_write:
                    continue
                api.create_product(candidate)
                pcreates += 1
                remote_product = api.lookup_product(candidate.product_id)
                if remote_product is None:
                    raise ValueError("Product missing from Shopify after mutation")
            if allow_write:
                repository.save_product(candidate, remote_product)
            products += 1

            mapped_variant = repository.get_variant(candidate.variant_id)
            remote_variant = api.lookup_variant(remote_product, candidate.sku)
            if mapped_variant is not None and mapped_variant != (
                remote_product, remote_variant
            ):
                raise ValueError("Existing variant mapping conflicts with Shopify")
            if remote_variant is None:
                missing_v += 1
                if not allow_write:
                    continue
                api.create_variant(remote_product, candidate)
                vcreates += 1
                remote_variant = api.lookup_variant(remote_product, candidate.sku)
                if remote_variant is None:
                    raise ValueError("Variant missing from Shopify after mutation")
            if allow_write:
                repository.save_variant(candidate, remote_product, remote_variant)
            variants += 1
    return CatalogSyncSummary(
        inspected, products, variants, pcreates, vcreates, missing_p, missing_v
    )


def reconcile_catalog_batches(
    candidates: Iterable[CatalogCandidate],
    api: CatalogAPI,
    repository: CatalogRepository,
    *,
    batch_size: int = 100,
    allow_write: bool = False,
    lease=None,
    on_batch: Callable[[int, int, CatalogSyncSummary], None] | None = None,
) -> CatalogSyncSummary:
    """Process an approved, ordered selection sequentially in bounded batches.

    Progress is reported only after a whole batch succeeds. A failed batch
    propagates its exception and does not advance to subsequent batches.
    Restarts re-read Shopify and RDS identities instead of relying on offsets.
    """
    if batch_size < 1 or batch_size > 1000:
        raise ValueError("Catalog batch size must be between 1 and 1000")
    items = tuple(candidates)
    if len({item.product_id for item in items}) != len(items):
        raise ValueError("Duplicate product in catalog sync run")
    if allow_write and lease is None:
        raise ValueError("Write-enabled catalog sync requires a durable lease")
    totals = {field.name: 0 for field in fields(CatalogSyncSummary)}
    total_batches = (len(items) + batch_size - 1) // batch_size
    for start in range(0, len(items), batch_size):
        summary = reconcile_catalog(
            items[start:start + batch_size], api, repository,
            allow_write=allow_write, lease=lease,
        )
        for field in fields(CatalogSyncSummary):
            totals[field.name] += getattr(summary, field.name)
        if on_batch is not None:
            on_batch(start // batch_size + 1, total_batches, summary)
    return CatalogSyncSummary(**totals)
