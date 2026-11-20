"""Load only a successfully published Glue Shopify Silver snapshot into Redshift.

Run from the project root; preview by default. Applies a transactionally committed
full snapshot of the five Shopify staging tables plus a warehouse load ledger row.
Historical synthetic staging tables are never changed. No new AWS resources.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
import re
import time
from uuid import uuid4
from botocore.exceptions import ClientError
from dataclasses import dataclass

import boto3

TABLE_KEYS = {
    "shopify_customer_identity": ("shop_gid", "customer_id"),
    "shopify_live_order": ("shop_domain", "shopify_order_id"),
    "shopify_live_order_line": ("shop_domain", "shopify_order_id", "shopify_line_id"),
    "shopify_live_refund": ("shop_domain", "shopify_refund_id"),
    "shopify_live_refund_line": ("shop_domain", "shopify_refund_id", "shopify_refund_line_id"),
}
PREFIX = "warehouse/shopify_snapshots/v1/"
SCHEMA_VERSION = "shopify-live-silver-v1"
SOURCE_TYPE = "shopify_silver"
VALID_KEY = re.compile(r"^[A-Za-z0-9_./=-]+$")
VALID_BATCH = re.compile(r"^\d{8}T\d{6}Z$")
VALID_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{2,62}$")
VALID_ROLE = re.compile(r"^arn:aws:iam::[0-9]{12}:role/[A-Za-z0-9_+=,.@/-]+$")


@dataclass(frozen=True)
class Snapshot:
    key: str
    batch_id: str
    digest: str
    counts: dict[str, int]
    files: dict[str, list[str]]


def inspect_snapshot(s3, *, bucket: str, manifest_key: str) -> Snapshot:
    if not VALID_KEY.fullmatch(manifest_key) or not manifest_key.startswith(PREFIX):
        raise ValueError("Manifest key outside the trusted Silver namespace")
    raw = s3.get_object(Bucket=bucket, Key=manifest_key)["Body"].read()
    manifest = json.loads(raw)
    batch_id = manifest.get("batch_id")
    if not isinstance(batch_id, str) or not VALID_BATCH.fullmatch(batch_id):
        raise ValueError("Invalid Silver batch ID")
    root = f"{PREFIX}{batch_id}/"
    if manifest_key != root + "silver_manifest.json":
        raise ValueError("Manifest path does not agree with batch ID")
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unexpected Silver manifest version")
    if manifest.get("source") != "rds_jdbc_full_snapshot":
        raise ValueError("Expected Glue-validated RDS Silver snapshot")
    counts = manifest.get("counts")
    files = manifest.get("files", {}).get("silver")
    if not isinstance(counts, dict) or not isinstance(files, dict):
        raise ValueError("Missing reconciled Silver counts/files")
    if set(counts) != set(TABLE_KEYS) or set(files) != set(TABLE_KEYS):
        raise ValueError("Snapshot must contain exactly the five Shopify tables")
    expected = {}
    paths = {}
    for table in TABLE_KEYS:
        record = counts[table]
        if not isinstance(record, dict) or set(record) != {"source", "silver", "quarantined"}:
            raise ValueError(f"Invalid reconciliation for {table}")
        if any(type(record[f]) is not int or record[f] < 0
               for f in ("source", "silver", "quarantined")):
            raise ValueError(f"Invalid counts for {table}")
        if record["source"] != record["silver"] + record["quarantined"]:
            raise ValueError(f"Unreconciled snapshot for {table}")
        expected[table] = record["silver"]
        path_list = files[table]
        if not isinstance(path_list, list) or not path_list:
            raise ValueError(f"No published Silver parts for {table}")
        if len(path_list) != len(set(path_list)):
            raise ValueError(f"Duplicate published Silver parts for {table}")
        for key in path_list:
            if not isinstance(key, str) or not VALID_KEY.fullmatch(key):
                raise ValueError(f"Invalid Silver part path for {table}")
            if not key.startswith(root + "silver/" + table + "/") or not key.endswith(".json.gz"):
                raise ValueError(f"Silver part is outside its batch/table: {table}")
        paths[table] = sorted(path_list)
    return Snapshot(
        key=manifest_key, batch_id=batch_id,
        digest=hashlib.sha256(raw).hexdigest(), counts=expected, files=paths,
    )


def verify_silver_rows(s3, *, bucket: str, snapshot: Snapshot) -> None:
    """Read published gzip JSONL, reject corrupted/missing parts and duplicates."""
    for table, keys in snapshot.files.items():
        row_count = 0
        seen = set()
        for key in keys:
            obj = s3.get_object(Bucket=bucket, Key=key)
            with obj["Body"] as body:
                with gzip.GzipFile(fileobj=body, mode="rb") as stream:
                    for line in stream:
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError(f"Non-object Silver row in {table}")
                        ids = tuple(row.get(field) for field in TABLE_KEYS[table])
                        if any(value is None or value == "" for value in ids):
                            raise ValueError(f"Missing Silver business key in {table}")
                        if ids in seen:
                            raise ValueError(f"Duplicate Silver business key in {table}")
                        seen.add(ids)
                        row_count += 1
        if row_count != snapshot.counts[table]:
            raise ValueError(
                f"Silver row count mismatch for {table}: {row_count} != {snapshot.counts[table]}"
            )


def query(client, *, workgroup: str, database: str, sql: str, timeout: int = 180):
    response = client.execute_statement(
        WorkgroupName=workgroup, Database=database, Sql=sql,
    )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.describe_statement(Id=response["Id"])
        if status["Status"] in ("FAILED", "ABORTED"):
            raise RuntimeError("Redshift query failed: " + str(status.get("Error", ""))[:500])
        if status["Status"] == "FINISHED":
            if not status.get("HasResultSet"):
                return []
            result = client.get_statement_result(Id=response["Id"])
            return [
                [next(iter(field.values()), None) for field in row]
                for row in result.get("Records", [])
            ]
        time.sleep(2)
    raise TimeoutError("Redshift query state uncertain; inspect before retry")


def latest_silver_load(client, *, workgroup: str, database: str):
    rows = query(
        client, workgroup=workgroup, database=database,
        sql="SELECT manifest_key, manifest_sha256 FROM silver_staging.warehouse_load_ledger "
            "WHERE source_type = 'shopify_silver' ORDER BY loaded_at DESC, manifest_key DESC LIMIT 1",
    )
    return (str(rows[0][0]), str(rows[0][1])) if rows else None


def assert_newer_snapshot(snapshot: Snapshot, latest):
    if latest is None:
        return "NEW"
    old_key, old_digest = latest
    if snapshot.key == old_key:
        if snapshot.digest != old_digest:
            raise RuntimeError("Silver manifest changed after successful load")
        return "ALREADY_LOADED"
    old_batch = old_key[len(PREFIX):].split("/", 1)[0] if old_key.startswith(PREFIX) else ""
    if not VALID_BATCH.fullmatch(old_batch):
        raise RuntimeError("Unrecognized latest Shopify Silver ledger entry")
    if snapshot.batch_id < old_batch:
        raise RuntimeError("Refusing to replace newer warehouse data with an old snapshot")
    if snapshot.batch_id == old_batch:
        raise RuntimeError("Conflicting manifests for the same batch")
    return "NEW"


def make_copy(table: str, path: str, bucket: str, role_arn: str) -> str:
    # Only manifest-verified S3 keys reach SQL construction.
    return (
        f"COPY load_{table} FROM 's3://{bucket}/{path}' "
        f"IAM_ROLE '{role_arn}' FORMAT AS JSON 'auto' GZIP "
        "TIMEFORMAT 'auto' REGION 'eu-central-1'"
    )


def load_sql(snapshot: Snapshot, *, bucket: str, role_arn: str) -> list[str]:
    sqls = []
    for table, keys in snapshot.files.items():
        target = "silver_staging." + table
        temp = "load_" + table
        sqls.append(f"CREATE TEMP TABLE {temp} (LIKE {target})")
        for key in keys:
            sqls.append(make_copy(table, key, bucket, role_arn))
        # The pre-flight check read every published Silver row, so COPY errors
        # are the only expected reason for a different warehouse row count.
        sqls.append(f"DELETE FROM {target}")
        sqls.append(f"INSERT INTO {target} SELECT * FROM {temp}")
        sqls.append(f"DROP TABLE {temp}")
    # Marker is part of the SAME Redshift transaction as all five replacements.
    # A failed COPY aborts the transaction and preserves the previous snapshot.
    sqls.append(
        "INSERT INTO silver_staging.warehouse_load_ledger "
        "(load_id,manifest_key,manifest_sha256,source_type) VALUES "
        f"('{snapshot.digest}','{snapshot.key}','{snapshot.digest}','{SOURCE_TYPE}')"
    )
    return sqls


def execute_snapshot(client, *, snapshot: Snapshot, bucket: str,
                     role_arn: str, workgroup: str, database: str):
    sqls = load_sql(snapshot, bucket=bucket, role_arn=role_arn)
    if len(sqls) > 40:
        raise ValueError("Too many Silver COPY parts for a single Redshift Data API batch")
    response = client.batch_execute_statement(
        WorkgroupName=workgroup, Database=database, Sqls=sqls,
    )
    run_id = response["Id"]
    deadline = time.monotonic() + 1800
    while time.monotonic() < deadline:
        desc = client.describe_statement(Id=run_id)
        if desc["Status"] in ("FAILED", "ABORTED"):
            raise RuntimeError("Silver transaction failed: " + str(desc.get("Error", ""))[:500])
        if desc["Status"] == "FINISHED":
            return run_id
        time.sleep(5)
    raise TimeoutError("Silver warehouse transaction state uncertain; reconcile before retry")


def verify_warehouse(client, *, snapshot: Snapshot, workgroup: str, database: str):
    rows = query(client, workgroup=workgroup, database=database,
                 sql=" UNION ALL ".join(
                     f"SELECT '{table}' AS table_name, COUNT(*) AS row_count "
                     f"FROM silver_staging.{table}"
                     for table in TABLE_KEYS
                 ))
    actual = {str(name): int(value) for name, value in rows}
    if actual != snapshot.counts:
        raise RuntimeError(f"Warehouse reconciliation mismatch: {actual} != {snapshot.counts}")
    return actual



def discover_manifests(s3, *, bucket: str) -> list[str]:
    """Discover committed snapshots only; Spark part files are not commits."""
    results = []
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=PREFIX):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if (key.endswith("/silver_manifest.json") and
                    VALID_KEY.fullmatch(key) and key.startswith(PREFIX)):
                batch = key[len(PREFIX):].split("/", 1)[0]
                if VALID_BATCH.fullmatch(batch) and key == f"{PREFIX}{batch}/silver_manifest.json":
                    results.append(key)
    return sorted(set(results))


def process_published_snapshots(*, s3, data, bucket: str, role_arn: str,
                                workgroup: str, database: str,
                                manifest_keys: list[str], apply: bool) -> list[dict]:
    if not query(
        data, workgroup=workgroup, database=database,
        sql="SELECT nspname FROM pg_namespace WHERE nspname = 'silver_staging'",
    ):
        raise RuntimeError("Expected the existing silver_staging schema")
    ledger_rows = query(
        data, workgroup=workgroup, database=database,
        sql="SELECT manifest_key, manifest_sha256 FROM silver_staging.warehouse_load_ledger "
            "WHERE source_type = 'shopify_silver'",
    )
    ledger = {}
    for key, digest in ledger_rows:
        if key in ledger and ledger[key] != digest:
            raise RuntimeError("Warehouse ledger contains conflicting manifest records")
        ledger[str(key)] = str(digest)
    latest = latest_silver_load(data, workgroup=workgroup, database=database)
    outcomes = []
    for key in sorted(set(manifest_keys)):
        snapshot = inspect_snapshot(s3, bucket=bucket, manifest_key=key)
        if key in ledger:
            if ledger[key] != snapshot.digest:
                raise RuntimeError("Previously loaded Silver manifest changed: " + key)
            outcomes.append({"manifest": key, "status": "ALREADY_LOADED"})
            continue
        if latest is not None:
            old_batch = latest[0][len(PREFIX):].split("/", 1)[0]
            if snapshot.batch_id <= old_batch:
                raise RuntimeError("Refusing out-of-order or duplicate full-state snapshot")
        verify_silver_rows(s3, bucket=bucket, snapshot=snapshot)
        if not apply:
            outcomes.append({"manifest": key, "status": "PENDING", "counts": snapshot.counts})
            latest = (key, snapshot.digest)
            continue
        run_id = execute_snapshot(
            data, snapshot=snapshot, bucket=bucket, role_arn=role_arn,
            workgroup=workgroup, database=database,
        )
        actual = verify_warehouse(
            data, snapshot=snapshot, workgroup=workgroup, database=database,
        )
        outcomes.append({"manifest": key, "status": "LOADED", "run_id": run_id,
                         "counts": actual})
        latest = (key, snapshot.digest)
    return outcomes


def lambda_handler(event, context):
    """Invoked after a successful Glue job; catch up all unpublished warehouse batches."""
    bucket = os.environ["SILVER_BUCKET"]
    role_arn = os.environ["REDSHIFT_COPY_ROLE_ARN"]
    workgroup = os.environ.get("REDSHIFT_WORKGROUP", "ecommerce-analytics-demo")
    database = os.environ.get("REDSHIFT_DATABASE", "analytics")
    s3 = boto3.client("s3")
    data = boto3.client("redshift-data")
    # The Glue success event and coordinator reconciliation may both invoke
    # this function. Serialize them using the EXISTING DynamoDB table.
    owner = str(uuid4())
    table = boto3.resource("dynamodb").Table(
        os.environ.get("CONTROLLER_TABLE", "ecommerce-demo-phase5-controller")
    )
    try:
        table.put_item(
            Item={"id": "phase6-redshift-loader", "owner": owner,
                  "expires_at": int(time.time()) + 930},
            ConditionExpression="attribute_not_exists(id) OR expires_at < :now",
            ExpressionAttributeValues={":now": int(time.time())},
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            print(json.dumps({"status": "LOADER_BUSY", "reason": "another_invocation_owns_lease"}))
            return {"status": "BUSY"}
        raise
    try:
        keys = discover_manifests(s3, bucket=bucket)
        if not keys:
            raise RuntimeError("No completed Glue Silver manifests")
        dry_run = bool(event.get("dry_run", False))
        result = process_published_snapshots(
            s3=s3, data=data, bucket=bucket, role_arn=role_arn,
            workgroup=workgroup, database=database, manifest_keys=keys, apply=not dry_run,
        )
        print(json.dumps({"status": "WAREHOUSE_PREVIEW" if dry_run else "WAREHOUSE_CATCHUP",
                          "outcomes": result}, sort_keys=True))
        return {"loaded": sum(x["status"] == "LOADED" for x in result),
                "dry_run": dry_run, "outcomes": result}
    finally:
        table.delete_item(
            Key={"id": "phase6-redshift-loader"},
            ConditionExpression="#owner=:owner",
            ExpressionAttributeNames={"#owner": "owner"},
            ExpressionAttributeValues={":owner": owner},
        )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bucket", required=True)
    group = p.add_mutually_exclusive_group(required=True)
    group.add_argument("--manifest-key")
    group.add_argument("--catch-up", action="store_true")
    p.add_argument("--copy-role-arn", required=True)
    p.add_argument("--profile", default="ecommerce-admin")
    p.add_argument("--region", default="eu-central-1")
    p.add_argument("--workgroup", default="ecommerce-analytics-demo")
    p.add_argument("--database", default="analytics")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if not VALID_BUCKET.fullmatch(args.bucket) or not VALID_ROLE.fullmatch(args.copy_role_arn):
        p.error("Invalid bucket or Redshift S3-copy role ARN")
    if args.region != "eu-central-1" or args.database != "analytics":
        p.error("Unexpected AWS region or warehouse database")
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    s3 = session.client("s3")
    data = session.client("redshift-data")
    keys = discover_manifests(s3, bucket=args.bucket) if args.catch_up else [args.manifest_key]
    results = process_published_snapshots(
        s3=s3, data=data, bucket=args.bucket, role_arn=args.copy_role_arn,
        workgroup=args.workgroup, database=args.database,
        manifest_keys=keys, apply=args.apply,
    )
    print(json.dumps({"mode": "APPLY" if args.apply else "PREVIEW",
                      "snapshots": results}, indent=2), flush=True)


if __name__ == "__main__":
    main()
