"""Crash-recoverable, immutable S3 publishing for synthetic UTC intervals.

The single conditionally written state/head.json is the commit marker. Downstream
readers MUST follow its manifest chain; unreferenced staged files or candidate
manifests are not committed outputs. No Shopify or RDS mutations.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
from datetime import UTC, datetime

from botocore.exceptions import ClientError

DEFAULT_SYNTHETIC_PREFIX = "historical/online_retail_ii/synthetic/v1"


def _encoded_json(payload: object) -> bytes:
    return (json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            + "\n").encode("utf-8")


def deterministic_jsonl_gzip(rows: list[dict]) -> bytes:
    """Canonical gzip, mtime=0, so object bytes are identical on a retry."""
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0, filename="") as zipped:
        for row in rows:
            zipped.write(_encoded_json(row))
    return output.getvalue()


def _digest(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _fetch(s3, bucket: str, key: str) -> tuple[bytes, str] | None:
    try:
        result = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response["Error"]["Code"] in {"NoSuchKey", "404", "NotFound"}:
            return None
        raise
    stream = result["Body"]
    try:
        raw = stream.read()
    finally:
        stream.close()
    return raw, result["ETag"]


def _put_immutable(s3, bucket: str, key: str, body: bytes, content_type: str) -> str:
    """Create once; an existing key must contain byte-for-byte identical data."""
    try:
        s3.put_object(
            Bucket=bucket, Key=key, Body=body,
            ContentType=content_type, IfNoneMatch="*",
            Metadata={"sha256": _digest(body)},
        )
    except ClientError as error:
        if error.response["Error"]["Code"] not in {
            "PreconditionFailed", "412", "ConditionalRequestConflict", "409"
        }:
            raise
        existing = _fetch(s3, bucket, key)
        if existing is None or existing[0] != body:
            raise FileExistsError(f"Conflicting immutable synthetic object: {key}") from error
    checked = _fetch(s3, bucket, key)
    if checked is None or checked[0] != body:
        raise OSError(f"Failed to verify immutable S3 object: {key}")
    return _digest(body)


def _utc_string(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset().total_seconds() != 0:
        raise ValueError("Expected UTC-aware datetime")
    return value.isoformat().replace("+00:00", "Z")


def publish_synthetic_interval(
    s3,
    *,
    bucket: str,
    start_utc: datetime,
    cutoff_utc: datetime,
    orders: list[dict],
    items: list[dict],
    policy_sha256: str,
    calibration_sha256: str,
    source_bronze_sha256: str,
    approved_catalog_sha256: str,
    prefix: str = DEFAULT_SYNTHETIC_PREFIX,
) -> dict:
    """Publish a contiguous S3 interval; commit with conditional head update.

    Caller freezes cutoff once at invocation. A head CAS failure never commits
    overlapping output. Manifest files by themselves are NOT a commit.
    """
    start, cutoff = _utc_string(start_utc), _utc_string(cutoff_utc)
    if not start_utc < cutoff_utc:
        raise ValueError("Invalid interval")
    if not prefix.startswith("historical/online_retail_ii/synthetic/v1"):
        raise ValueError("Synthetic prefix must stay inside dedicated versioned namespace")
    for sha in (
        policy_sha256, calibration_sha256, source_bronze_sha256, approved_catalog_sha256
    ):
        if len(sha) != 64 or any(ch not in "0123456789abcdef" for ch in sha):
            raise ValueError("All lineage hashes must be lowercase SHA-256")
    ids = [order["order_id"] for order in orders]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate synthetic order IDs")
    if any(not start <= order["event_time_utc"].replace("+00:00", "Z") < cutoff for order in orders):
        raise ValueError("Order outside its declared UTC interval")
    if any(item["order_id"] not in set(ids) for item in items):
        raise ValueError("Orphan synthetic order item")
    if len({item["order_item_id"] for item in items}) != len(items):
        raise ValueError("Duplicate synthetic item IDs")

    root = prefix.strip("/")
    head_key = f"{root}/state/head.json"
    head_record = _fetch(s3, bucket, head_key)
    prior_head = json.loads(head_record[0]) if head_record else None
    expected_origin = "2026-01-01T00:00:00Z"
    lineage = {
        "policy_sha256": policy_sha256,
        "calibration_sha256": calibration_sha256,
        "source_bronze_sha256": source_bronze_sha256,
        "approved_catalog_sha256": approved_catalog_sha256,
    }
    if prior_head:
        if prior_head["cutoff_utc"] == cutoff and prior_head["start_utc"] == start:
            if any(prior_head.get(name) != value for name, value in lineage.items()):
                raise ValueError("Committed interval has different lineage")
            manifest = _fetch(s3, bucket, prior_head["manifest_key"])
            if manifest is None or _digest(manifest[0]) != prior_head["manifest_sha256"]:
                raise OSError("Committed synthetic manifest is missing or corrupt")
            return json.loads(manifest[0])
        if start != prior_head["cutoff_utc"]:
            raise ValueError("Backfill must continue from previous committed UTC cutoff")
        if any(prior_head.get(name) != value for name, value in lineage.items()):
            raise ValueError("Cannot extend backfill with different source/model lineage")
    elif start != expected_origin:
        raise ValueError("Initial backfill must begin at 2026-01-01T00:00:00Z")

    interval_key = (
        f"{start.replace(':', '').replace('-', '')}__"
        f"{cutoff.replace(':', '').replace('-', '')}"
    )
    base = f"{root}/intervals/{interval_key}"
    rows = {
        "orders": (orders, f"{base}/orders.jsonl.gz"),
        "order_items": (items, f"{base}/order_items.jsonl.gz"),
    }
    assets = {}
    for name, (records, key) in rows.items():
        raw = deterministic_jsonl_gzip(records)
        assets[name] = {
            "key": key,
            "rows": len(records),
            "sha256": _put_immutable(s3, bucket, key, raw, "application/gzip"),
        }
    manifest = {
        "schema_version": "synthetic-2026-backfill-manifest-v1",
        "start_utc": start,
        "cutoff_utc": cutoff,
        "prior_manifest_key": prior_head["manifest_key"] if prior_head else None,
        "lineage": lineage,
        "files": assets,
        "commit_semantics": "Only reachable from state/head.json after successful CAS",
    }
    manifest_key = f"{base}/manifest.json"
    manifest_raw = _encoded_json(manifest)
    manifest_sha = _put_immutable(s3, bucket, manifest_key, manifest_raw, "application/json")
    next_head = {
        "start_utc": start,
        "cutoff_utc": cutoff,
        "manifest_key": manifest_key,
        "manifest_sha256": manifest_sha,
        **lineage,
    }
    put_args = {
        "Bucket": bucket,
        "Key": head_key,
        "Body": _encoded_json(next_head),
        "ContentType": "application/json",
    }
    if head_record is None:
        put_args["IfNoneMatch"] = "*"
    else:
        put_args["IfMatch"] = head_record[1]
    # Atomic compare-and-swap: publication is not committed until this succeeds.
    s3.put_object(**put_args)
    verified = _fetch(s3, bucket, head_key)
    if verified is None or json.loads(verified[0]) != next_head:
        raise OSError("Synthetic head checkpoint verification failed")
    return manifest
