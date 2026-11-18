"""Validate a checksum-pinned, explicitly reviewed historical catalog subset.

Approval only establishes the listed stock codes as eligible for an authorized
Shopify run. It does not infer Shopify GIDs, prices, or currencies.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable

from ecommerce.application.use_cases.catalog_selection import CatalogCandidate


def approved_catalog(s3, bucket: str, key: str, checksum: str) -> frozenset[str]:
    """Load explicit approved SKU list from S3, reject mutable/unverified data."""
    if not key.startswith("historical/online_retail_ii/catalog_approval/v1/"):
        raise ValueError("Catalog approvals must use the dedicated S3 prefix")
    if len(checksum) != 64 or any(x not in "0123456789abcdef" for x in checksum):
        raise ValueError("Expected a lowercase SHA-256 approval checksum")
    stream = s3.get_object(Bucket=bucket, Key=key)["Body"]
    try:
        body = stream.read()
    finally:
        stream.close()
    if hashlib.sha256(body).hexdigest() != checksum:
        raise ValueError("Catalog approval manifest checksum mismatch")
    manifest = json.loads(body)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "catalog-approval-v1":
        raise ValueError("Unexpected catalog approval schema")
    items = manifest.get("approved_stock_codes")
    if not isinstance(items, list) or not items:
        raise ValueError("Approved stock-code list is empty")
    if not all(isinstance(item, str) and item.strip() == item and item for item in items):
        raise ValueError("Invalid approved stock code")
    if len(set(items)) != len(items):
        raise ValueError("Duplicate approved stock codes")
    return frozenset(items)


def selected_catalog(
    candidates: Iterable[CatalogCandidate], *, limit: int,
    allow_write: bool, approved: frozenset[str] | None = None,
) -> tuple[CatalogCandidate, ...]:
    if limit < 0:
        raise ValueError("Catalog limit must be non-negative")
    candidates = tuple(candidates)
    if allow_write:
        if not approved:
            raise ValueError("Write-enabled sync requires reviewed catalog approvals")
        known = {item.source_stock_code for item in candidates}
        if not approved.issubset(known):
            raise ValueError("Catalog approvals reference unknown or ineligible codes")
        candidates = tuple(c for c in candidates if c.source_stock_code in approved)
    return candidates[:limit]

def manifest_from_audit(report: dict) -> bytes:
    """Build a deterministic explicit allowlist of only unflagged source SKUs.

    The operator's policy approves products without detected audit flags.
    Fail closed on missing/partial or internally inconsistent report evidence.
    """
    rows = report.get("review_queue")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Missing catalog audit review queue")
    source = report.get("source")
    if not isinstance(source, dict) or not source.get("bronze_source_sha256"):
        raise ValueError("Missing source lineage evidence")
    total = report.get("source_products")
    candidates = report.get("candidate_products")
    exclusions = report.get("excluded_by_class")
    if (
        not isinstance(total, int) or not isinstance(candidates, int)
        or not isinstance(exclusions, dict)
        or total != candidates + sum(exclusions.values())
        or len(rows) != candidates
    ):
        raise ValueError("Catalog audit population does not reconcile")
    codes = []
    flagged = 0
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Malformed catalog review row")
        code = row.get("stock_code")
        reasons = row.get("review_reasons")
        needs_review = row.get("needs_manual_review")
        if not isinstance(code, str) or not code or code.strip() != code:
            raise ValueError("Invalid catalog stock code")
        if not isinstance(reasons, list) or not isinstance(needs_review, bool):
            raise ValueError("Missing catalog review reasons")
        if needs_review != bool(reasons):
            raise ValueError("Conflicting catalog audit flags")
        if needs_review:
            flagged += 1
        else:
            codes.append(code)
    if (
        len(set(row["stock_code"] for row in rows)) != len(rows)
        or flagged != report.get("manual_review_required")
        or len(codes) != report.get("without_detected_flags")
        or not codes
    ):
        raise ValueError("Catalog audit review counts do not reconcile")
    manifest = {
        "schema_version": "catalog-approval-v1",
        "approved_stock_codes": sorted(codes),
    }
    return (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\\n").encode("utf-8")
