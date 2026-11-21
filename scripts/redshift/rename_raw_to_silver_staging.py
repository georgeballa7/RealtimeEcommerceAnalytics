"""Guarded one-time Redshift schema rename from raw to silver_staging.

Dry-run by default. An existing populated historical schema must be renamed
in place, never copied or replaced. Does not load RDS or modify S3.
"""
from __future__ import annotations

import argparse
import json
import time

import boto3


def query(data, workgroup, database, sql):
    result = data.execute_statement(WorkgroupName=workgroup, Database=database, Sql=sql)
    statement_id = result["Id"]
    for _ in range(120):
        status = data.describe_statement(Id=statement_id)
        if status["Status"] == "FINISHED":
            if not status.get("HasResultSet"):
                return []
            rows = data.get_statement_result(Id=statement_id)["Records"]
            return [[next(iter(cell.values()), None) for cell in row] for row in rows]
        if status["Status"] in ("FAILED", "ABORTED"):
            raise RuntimeError(f"Redshift SQL failed: {status.get('Error', 'unknown')}")
        time.sleep(2)
    raise TimeoutError("Redshift operation status uncertain; inspect Data API before retry")


def schema_tables(data, workgroup, database, schema):
    return query(
        data, workgroup, database,
        "SELECT tablename FROM pg_tables WHERE schemaname = '" + schema + "' ORDER BY tablename"
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--profile", default="ecommerce-admin")
    p.add_argument("--region", default="eu-central-1")
    p.add_argument("--workgroup", default="ecommerce-analytics-demo")
    p.add_argument("--database", default="analytics")
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    data = boto3.Session(profile_name=args.profile, region_name=args.region).client("redshift-data")

    existing = {
        str(row[0]) for row in query(
            data, args.workgroup, args.database,
            "SELECT nspname FROM pg_namespace WHERE nspname IN ('raw','silver_staging')"
        )
    }
    old_tables = {str(row[0]) for row in schema_tables(
        data, args.workgroup, args.database, "raw"
    )} if "raw" in existing else set()
    new_tables = {str(row[0]) for row in schema_tables(
        data, args.workgroup, args.database, "silver_staging"
    )} if "silver_staging" in existing else set()
    required = {"synthetic_orders", "synthetic_order_items", "synthetic_cancellations",
                "warehouse_load_ledger"}
    preview = {
        "operation": "ALTER SCHEMA \"raw\" RENAME TO silver_staging",
        "schemas": sorted(existing),
        "raw_tables": sorted(old_tables),
        "silver_staging_tables": sorted(new_tables),
        "required_tables_found": required.issubset(old_tables),
        "mode": "APPLY" if args.apply else "PREVIEW",
    }
    print(json.dumps(preview, indent=2), flush=True)
    if existing == {"silver_staging"} and required.issubset(new_tables):
        print("Already migrated; no action.")
        return
    if existing != {"raw"} or not required.issubset(old_tables):
        raise RuntimeError(
            "Unsafe migration state: expected existing raw only, including all "
            "three historical synthetic tables and warehouse_load_ledger."
        )
    if not args.apply:
        return

    query(data, args.workgroup, args.database, 'ALTER SCHEMA "raw" RENAME TO silver_staging')
    renamed = {str(row[0]) for row in schema_tables(
        data, args.workgroup, args.database, "silver_staging"
    )}
    if not old_tables.issubset(renamed):
        raise RuntimeError("Migration executed but table inventory does not match; investigate.")
    print(json.dumps({"status": "RENAMED", "tables": sorted(renamed)}, indent=2))


if __name__ == "__main__":
    main()
