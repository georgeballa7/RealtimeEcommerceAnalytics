"""Manifest-pinned S3 -> Redshift Serverless COPY + transactional upsert.

Dry run by default. Uses Redshift Data API/IAM auth (no plaintext credentials).
No source mutation. The source S3 backfill is deterministic synthetic data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from pathlib import Path

import boto3

SYNTHETIC_HEAD = "historical/online_retail_ii/synthetic/v1/state/head.json"
SYNTHETIC_TABLES = {
    "orders": ("landing.synthetic_orders", "order_id"),
    "order_items": ("landing.synthetic_order_items", "order_item_id"),
    "cancellations": ("landing.synthetic_cancellations", "event_id"),
}
LIVE_TABLES = {
    "shopify_live_order": "landing.shopify_live_order",
    "shopify_live_order_line": "landing.shopify_live_order_line",
    "shopify_live_refund": "landing.shopify_live_refund",
    "shopify_live_refund_line": "landing.shopify_live_refund_line",
    "shopify_customer_identity": "landing.shopify_customer_identity",
}
SAFE_S3_KEY = re.compile(r"^[a-zA-Z0-9_./=-]+$")
SAFE_ARN = re.compile(r"^arn:aws:iam::[0-9]{12}:role/[a-zA-Z0-9_+=,.@/-]+$")
SAFE_DATABASE = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def read_json(s3, bucket: str, key: str) -> dict:
    if not SAFE_S3_KEY.fullmatch(key):
        raise ValueError("Unsafe S3 path")
    return json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())


def verified_source_file(s3, bucket: str, spec: dict) -> str:
    """Verify exact compressed bytes; never silently migrate a changed file."""
    key, expected = spec["key"], spec["sha256"]
    if not SAFE_S3_KEY.fullmatch(key) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("Malformed S3 manifest")
    sha = hashlib.sha256()
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    try:
        for chunk in body.iter_chunks(chunk_size=1024 * 1024):
            sha.update(chunk)
    finally:
        body.close()
    if sha.hexdigest() != expected:
        raise ValueError("S3 backfill checksum mismatch: " + key)
    return key


def synthetic_manifests(s3, bucket: str) -> list[tuple[str, dict]]:
    head = read_json(s3, bucket, SYNTHETIC_HEAD)
    key = head["manifest_key"]
    visited = set()
    manifests = []
    while key:
        if key in visited or not key.startswith(
            "historical/online_retail_ii/synthetic/v1/intervals/"
        ):
            raise ValueError("Invalid/cyclic synthetic manifest chain")
        visited.add(key)
        manifest = read_json(s3, bucket, key)
        if manifest.get("schema_version") != "synthetic-2026-backfill-manifest-v2":
            raise ValueError("Unsupported synthetic schema")
        if set(manifest.get("files", {})) != set(SYNTHETIC_TABLES):
            raise ValueError("Synthetic files do not match contract")
        if manifest.get("lineage", {}).get("policy_sha256") != head["policy_sha256"]:
            raise ValueError("Synthetic policy drift")
        manifests.append((key, manifest))
        key = manifest.get("prior_manifest_key")
    manifests.reverse()
    for i in range(1, len(manifests)):
        if manifests[i - 1][1]["cutoff_utc"] != manifests[i][1]["start_utc"]:
            raise ValueError("Synthetic time ranges have a gap/overlap")
    if manifests[-1][1]["cutoff_utc"] != head["cutoff_utc"]:
        raise ValueError("Synthetic head cutoff mismatch")
    return manifests


def run_data_api(client, *, workgroup: str, database: str, statements: list[str],
                 timeout: int = 1800) -> dict:
    """BatchExecuteStatement executes SQLs sequentially as one transaction."""
    if not statements:
        return {"status": "EMPTY"}
    response = client.batch_execute_statement(
        WorkgroupName=workgroup, Database=database, Sqls=statements,
    )
    execution_id = response["Id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.describe_statement(Id=execution_id)
        if status["Status"] in ("FAILED", "ABORTED"):
            raise RuntimeError("Redshift execution failed: " + str(status.get("Error", ""))[:500])
        if status["Status"] == "FINISHED":
            return {"status": "FINISHED", "id": execution_id}
        time.sleep(5)
    raise TimeoutError("Redshift statement state uncertain; reconcile before replay")


def landing_schema_exists(client, *, workgroup: str, database: str,
                      timeout: int = 120) -> bool:
    """Check the catalog before adding CREATE SCHEMA to the DDL batch.

    The target warehouse rejected CREATE SCHEMA IF NOT EXISTS; the explicit
    catalog check makes repeated bootstrap runs safe without that syntax.
    """
    execution_id = client.execute_statement(
        WorkgroupName=workgroup, Database=database,
        Sql="SELECT 1 FROM pg_namespace WHERE nspname = 'landing'",
    )["Id"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.describe_statement(Id=execution_id)
        if status["Status"] in ("FAILED", "ABORTED"):
            raise RuntimeError(
                "Redshift schema lookup failed: " + str(status.get("Error", ""))[:500]
            )
        if status["Status"] == "FINISHED":
            result = client.get_statement_result(Id=execution_id)
            return bool(result.get("Records"))
        time.sleep(2)
    raise TimeoutError("Redshift schema lookup state uncertain; reconcile before retry")


def sql_copy(table: str, key: str, bucket: str, role_arn: str) -> str:
    if not SAFE_S3_KEY.fullmatch(key):
        raise ValueError("Unsafe S3 path")
    return (
        f"COPY {table} FROM 's3://{bucket}/{key}' "
        f"IAM_ROLE '{role_arn}' FORMAT AS JSON 'auto' GZIP "
        "TIMEFORMAT 'auto' REGION 'eu-central-1'"
    )


def synthetic_statements(bucket: str, role_arn: str, manifest: dict) -> list[str]:
    sqls = []
    for source, (target, primary_key) in SYNTHETIC_TABLES.items():
        key = manifest["files"][source]["key"]
        temp = "load_" + source
        sqls.extend([
            f"CREATE TEMP TABLE {temp} (LIKE {target})",
            sql_copy(temp, key, bucket, role_arn),
            f"DELETE FROM {target} USING {temp} WHERE {target}.{primary_key} = {temp}.{primary_key}",
            f"INSERT INTO {target} SELECT * FROM {temp}",
            f"DROP TABLE {temp}",
        ])
    return sqls


def live_statements(bucket: str, role_arn: str, manifest: dict) -> list[str]:
    if set(manifest.get("files", {})) != set(LIVE_TABLES):
        raise ValueError("Incomplete RDS source snapshot")
    sqls = []
    for name, target in LIVE_TABLES.items():
        temp = "load_" + name
        sqls.extend([
            f"CREATE TEMP TABLE {temp} (LIKE {target})",
            sql_copy(temp, manifest["files"][name]["key"], bucket, role_arn),
            f"DELETE FROM {target}",
            f"INSERT INTO {target} SELECT * FROM {temp}",
            f"DROP TABLE {temp}",
        ])
    return sqls


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--workgroup", default="ecommerce-analytics-demo")
    parser.add_argument("--database", default="analytics")
    parser.add_argument("--copy-role-arn", required=True)
    parser.add_argument("--profile", default="ecommerce-admin")
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--live-manifest", help="Committed RDS snapshot manifest key")
    parser.add_argument("--apply", action="store_true", help="Explicit warehouse mutation")
    args = parser.parse_args()
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{2,62}", args.bucket):
        parser.error("Invalid bucket")
    if not SAFE_ARN.fullmatch(args.copy_role_arn) or not SAFE_DATABASE.fullmatch(args.database):
        parser.error("Invalid role or database")
    if args.region != "eu-central-1":
        parser.error("AWS region differs from pinned architecture")
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    s3 = session.client("s3")
    manifests = synthetic_manifests(s3, args.bucket)
    plans = []
    for manifest_key, manifest in manifests:
        for spec in manifest["files"].values():
            verified_source_file(s3, args.bucket, spec)
        plans.append((manifest_key, manifest, "synthetic"))
    if args.live_manifest:
        if not args.live_manifest.startswith("warehouse/live_exports/v1/"):
            parser.error("Unapproved live manifest key")
        live = read_json(s3, args.bucket, args.live_manifest)
        if live.get("schema_version") != "rds-live-snapshot-v1":
            parser.error("Unknown RDS snapshot version")
        for spec in live["files"].values():
            verified_source_file(s3, args.bucket, spec)
        plans.append((args.live_manifest, live, "live"))
    summary = [{"manifest": key, "kind": kind,
                "rows": {k: v["rows"] for k, v in m["files"].items()}}
               for key, m, kind in plans]
    print(json.dumps({"mode": "APPLY" if args.apply else "PREVIEW", "plans": summary},
                     indent=2), flush=True)
    if not args.apply:
        return
    data = session.client("redshift-data")
    ddl_path = Path(__file__).resolve().parents[2] / "analytics/redshift/landing_schema.sql"
    ddl = [statement.strip() for statement in ddl_path.read_text().split(";") if statement.strip()]
    if not landing_schema_exists(data, workgroup=args.workgroup, database=args.database):
        ddl.insert(0, 'CREATE SCHEMA landing')
    run_data_api(data, workgroup=args.workgroup, database=args.database, statements=ddl)
    for manifest_key, manifest, kind in plans:
        src = (synthetic_statements(args.bucket, args.copy_role_arn, manifest)
               if kind == "synthetic" else live_statements(args.bucket, args.copy_role_arn, manifest))
        # Marker is committed in same transaction as the source COPY/upsert.
        manifest_sha = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        escaped = manifest_key.replace("'", "''")
        src.append(
            "INSERT INTO landing.warehouse_load_ledger (load_id,manifest_key,manifest_sha256,source_type) "
            f"SELECT '{manifest_sha}','{escaped}','{manifest_sha}','{kind}' "
            f"WHERE NOT EXISTS (SELECT 1 FROM landing.warehouse_load_ledger WHERE load_id='{manifest_sha}')"
        )
        outcome = run_data_api(data, workgroup=args.workgroup, database=args.database,
                               statements=src)
        print(json.dumps({"manifest": manifest_key, **outcome}), flush=True)


if __name__ == "__main__":
    main()
