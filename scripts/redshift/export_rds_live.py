"""Read-only repeatable-read RDS snapshot -> immutable, manifest-committed S3.

Full snapshot of the small operational model. Does NOT start RDS; call only
after controller reports DB available. No Shopify or Redshift mutations.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal

import boto3
import psycopg
from botocore.exceptions import ClientError

TABLES = {
    "shopify_live_order": "shop_domain, shopify_order_id",
    "shopify_live_order_line": "shop_domain, shopify_order_id, shopify_line_id",
    "shopify_live_refund": "shop_domain, shopify_refund_id",
    "shopify_live_refund_line": "shop_domain, shopify_refund_id, shopify_refund_line_id",
    "shopify_customer_identity": "shop_gid, customer_id",
}
SAFE_SNAPSHOT = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")


def deterministic_gzip(rows: list[dict]) -> bytes:
    data = io.BytesIO()
    with gzip.GzipFile(fileobj=data, mode="wb", mtime=0) as stream:
        for row in rows:
            stream.write((json.dumps(row, sort_keys=True, separators=(",", ":"),
                                     default=str, ensure_ascii=False) + "\n").encode("utf-8"))
    return data.getvalue()


def put_immutable(s3, bucket: str, key: str, payload: bytes) -> str:
    sha = hashlib.sha256(payload).hexdigest()
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=payload,
                      ContentType="application/json", IfNoneMatch="*")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in ("PreconditionFailed", "412"):
            raise
        old = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        if hashlib.sha256(old).hexdigest() != sha:
            raise RuntimeError("Conflicting RDS snapshot object: " + key) from exc
    return sha


def collect_snapshot(conn) -> dict[str, list[dict]]:
    result = {}
    # Explicit read-only repeatable-read snapshot across all tables.
    conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    for table, order_by in TABLES.items():
        with conn.cursor() as cur:
            cur.execute(f"SELECT * FROM {table} ORDER BY {order_by}")
            columns = [desc.name for desc in cur.description]
            rows = [dict(zip(columns, row)) for row in cur]
        result[table] = rows
    conn.rollback()  # End read-only transaction; no writes.
    return result


def export_snapshot(s3, bucket: str, snapshot_id: str, tables: dict) -> dict:
    if not SAFE_SNAPSHOT.fullmatch(snapshot_id):
        raise ValueError("Snapshot must be YYYYMMDDTHHMMSSZ")
    prefix = f"warehouse/live_exports/v1/{snapshot_id}"
    files = {}
    for table in TABLES:
        rows = tables[table]
        compressed = deterministic_gzip(rows)
        key = f"{prefix}/{table}.jsonl.gz"
        sha = put_immutable(s3, bucket, key, compressed)
        files[table] = {"key": key, "rows": len(rows), "sha256": sha}
    manifest = {
        "schema_version": "rds-live-snapshot-v1",
        "snapshot_id": snapshot_id,
        "source": "ecommerce_db",
        "files": files,
        "consistency": "single-postgresql-repeatable-read-transaction",
    }
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
    put_immutable(s3, bucket, f"{prefix}/manifest.json", encoded)
    return {"manifest": f"{prefix}/manifest.json", "rows": {
        k: len(v) for k, v in tables.items()}, "sha256": hashlib.sha256(encoded).hexdigest()}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bucket", default=os.environ.get("S3_LANDING_BUCKET"))
    p.add_argument("--snapshot-id")
    p.add_argument("--apply", action="store_true")
    a = p.parse_args()
    if not a.bucket:
        p.error("S3 bucket required")
    snapshot_id = a.snapshot_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    if not SAFE_SNAPSHOT.fullmatch(snapshot_id):
        p.error("Invalid snapshot ID")
    if not a.apply:
        print(json.dumps({"mode": "PREVIEW", "snapshot_id": snapshot_id,
                          "tables": list(TABLES)}, sort_keys=True))
        return
    cert = os.environ.get("DB_SSLROOTCERT", "/etc/ssl/certs/rds-global-bundle.pem")
    if not os.path.isfile(cert):
        p.error("Missing RDS CA trust bundle")
    with psycopg.connect(
        host=os.environ["DB_HOST"], dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        port=int(os.environ.get("DB_PORT", 5432)),
        sslmode="verify-full", sslrootcert=cert, connect_timeout=10,
    ) as connection:
        tables = collect_snapshot(connection)
    result = export_snapshot(boto3.client("s3"), a.bucket, snapshot_id, tables)
    print(json.dumps({"mode": "APPLY", **result}, sort_keys=True))


if __name__ == "__main__":
    main()
