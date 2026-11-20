"""Phase 6: RDS JDBC -> immutable S3 Bronze -> validated S3 Silver.

AWS Glue PySpark, on-demand only. This is a full-state extract, *not* CDC.
Run only after the Phase 5 projector has completed and database writes are quiescent.
Separate JDBC table reads do not guarantee one transaction snapshot: cross-table
checks are an additional guard, not a replacement for snapshot isolation.
"""
from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone

import boto3
from pyspark.sql import SparkSession, functions as F, types as T

TABLE_KEYS = {
    "shopify_live_order": ["shop_domain", "shopify_order_id"],
    "shopify_live_order_line": ["shop_domain", "shopify_order_id", "shopify_line_id"],
    "shopify_live_refund": ["shop_domain", "shopify_refund_id"],
    "shopify_live_refund_line": ["shop_domain", "shopify_refund_id", "shopify_refund_line_id"],
    "shopify_customer_identity": ["shop_gid", "customer_id"],
}
# Output contract mirrors analytics/redshift/silver_staging_schema.sql.
CONTRACT = {
    "shopify_live_order": [
        ("shop_domain", "string"), ("shopify_order_id", "long"),
        ("shopify_customer_gid", "string"), ("currency", "string"),
        ("created_at_source", "timestamp"), ("updated_at_source", "timestamp"),
        ("cancelled_at", "timestamp"), ("financial_status", "string"),
        ("fulfillment_status", "string"), ("gross_total", "decimal(18,2)"),
        ("current_total", "decimal(18,2)"), ("synthetic", "boolean"),
        ("ingested_at", "timestamp"),
    ],
    "shopify_live_order_line": [
        ("shop_domain", "string"), ("shopify_order_id", "long"),
        ("shopify_line_id", "long"), ("shopify_variant_id", "long"),
        ("sku", "string"), ("quantity", "int"), ("unit_price", "decimal(18,2)"),
        ("discount_total", "decimal(18,2)"), ("updated_at_source", "timestamp"),
    ],
    "shopify_live_refund": [
        ("shop_domain", "string"), ("shopify_refund_id", "long"),
        ("shopify_order_id", "long"), ("created_at_source", "timestamp"),
        ("refund_line_subtotal", "decimal(18,2)"), ("payload_sha256", "string"),
    ],
    "shopify_live_refund_line": [
        ("shop_domain", "string"), ("shopify_refund_id", "long"),
        ("shopify_refund_line_id", "long"), ("shopify_order_line_id", "long"),
        ("quantity", "int"), ("subtotal", "decimal(18,2)"),
    ],
    "shopify_customer_identity": [
        ("shop_gid", "string"), ("customer_id", "string"),
        ("source_system", "string"), ("source_customer_id", "string"),
        ("shopify_customer_gid", "string"), ("status", "string"),
        ("updated_at", "timestamp"),
    ],
}
# Hard rejects preserve Bronze evidence; anomalies are never silently removed.
REQUIRED_NON_NULL = {
    "shopify_live_order": ["shop_domain", "shopify_order_id", "created_at_source", "currency", "gross_total", "current_total"],
    "shopify_live_order_line": ["shop_domain", "shopify_order_id", "shopify_line_id", "quantity", "unit_price", "discount_total"],
    "shopify_live_refund": ["shop_domain", "shopify_refund_id", "shopify_order_id"],
    "shopify_live_refund_line": ["shop_domain", "shopify_refund_id", "shopify_refund_line_id", "quantity", "subtotal"],
    "shopify_customer_identity": ["shop_gid", "customer_id", "shopify_customer_gid", "status"],
}
VARCHAR_LIMITS = {
    "shopify_live_order": {
        "shop_domain": 255, "shopify_customer_gid": 255, "currency": 3,
        "financial_status": 80, "fulfillment_status": 80,
    },
    "shopify_live_order_line": {"shop_domain": 255, "sku": 128},
    "shopify_live_refund": {"shop_domain": 255, "payload_sha256": 64},
    "shopify_live_refund_line": {"shop_domain": 255},
    "shopify_customer_identity": {
        "shop_gid": 255, "customer_id": 40, "source_system": 80,
        "source_customer_id": 80, "shopify_customer_gid": 255, "status": 40,
    },
}
SAFE_BATCH = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
SCHEMA_VERSION = "shopify-live-silver-v1"


def to_contract(frame, table):
    missing = set(name for name, _ in CONTRACT[table]) - set(frame.columns)
    if missing:
        raise ValueError(f"{table} missing source columns: {sorted(missing)}")
    columns = []
    for name, target_type in CONTRACT[table]:
        value = F.col(name)
        if target_type == "string":
            value = F.trim(value.cast("string"))
        columns.append(value.cast(target_type).alias(name))
    return frame.select(*columns)


def add_reasons(frame, table):
    reasons = [F.when(F.col(c).isNull() | (F.trim(F.col(c)) == "" if c in ("shop_domain", "currency", "shop_gid", "customer_id", "shopify_customer_gid", "status") else F.lit(False)),
                      F.lit("missing_or_invalid_" + c)) for c in REQUIRED_NON_NULL[table]]
    reasons += [F.when(F.length(F.col(name)) > max_length, F.lit("varchar_overflow_" + name))
                for name, max_length in VARCHAR_LIMITS[table].items()]
    if table == "shopify_live_order":
        reasons += [
            F.when(~F.col("currency").rlike("^[A-Z]{3}$"), F.lit("invalid_currency")),
            F.when(F.col("gross_total") < 0, F.lit("negative_gross_total")),
            F.when(F.col("current_total") < 0, F.lit("negative_current_total")),
        ]
    elif table == "shopify_live_order_line":
        reasons += [
            F.when(F.col("quantity") <= 0, F.lit("nonpositive_order_quantity")),
            F.when(F.col("unit_price") < 0, F.lit("negative_unit_price")),
            F.when(F.col("discount_total") < 0, F.lit("negative_discount")),
        ]
    elif table == "shopify_live_refund_line":
        reasons += [
            F.when(F.col("quantity") <= 0, F.lit("nonpositive_refund_quantity")),
            F.when(F.col("subtotal") < 0, F.lit("negative_refund_subtotal")),
        ]
    return frame.withColumn("dq_reasons", F.filter(F.array(*reasons), lambda value: value.isNotNull()))


def ensure_unique(frame, keys, table):
    # Explicit FAIL (rather than nondeterministic dropDuplicates) on colliding keys.
    duplicated = frame.groupBy(*keys).count().where(F.col("count") > 1).limit(1).count()
    if duplicated:
        raise ValueError(f"Duplicate {table} business keys; preserve source and investigate")


def write_partitioned(frame, uri):
    # Spark writes multiple part files, not one in-memory serialized object.
    frame.write.mode("errorifexists").option("compression", "gzip").json(uri)


def discover_part_files(s3, bucket, prefix):
    files = []
    pager = s3.get_paginator("list_objects_v2")
    for page in pager.paginate(Bucket=bucket, Prefix=prefix.rstrip("/") + "/"):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            if "/part-" in key and (key.endswith(".json.gz") or key.endswith(".parquet")):
                files.append(key)
    return sorted(files)


def main():
    parser = argparse.ArgumentParser()
    for flag in ("bucket", "db-host", "db-name", "secret-arn", "batch-id"):
        parser.add_argument("--" + flag, required=True)
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--db-port", type=int, default=5432)
    args, _ = parser.parse_known_args()
    if not SAFE_BATCH.fullmatch(args.batch_id):
        parser.error("batch-id must be YYYYMMDDTHHMMSSZ")
    spark = SparkSession.builder.appName("ecommerce-phase6-shopify-bronze-to-silver").getOrCreate()
    spark.conf.set("spark.sql.session.timeZone", "UTC")
    aws = boto3.Session(region_name=args.region)
    s3 = aws.client("s3")
    secret = json.loads(aws.client("secretsmanager").get_secret_value(
        SecretId=args.secret_arn)["SecretString"])
    password = secret["password"]
    db_user = secret["username"]
    jdbc = f"jdbc:postgresql://{args.db_host}:{args.db_port}/{args.db_name}?sslmode=require"
    prefix = f"warehouse/shopify_snapshots/v1/{args.batch_id}"
    counts = {}
    valid = {}
    rejects = {}
    # Read each table without JDBC parallel partitioning to protect the small RDS.
    for table in TABLE_KEYS:
        raw = (spark.read.format("jdbc").option("url", jdbc)
               .option("dbtable", f"public.{table}")
               .option("user", db_user).option("password", password)
               .option("driver", "org.postgresql.Driver").load())
        raw.persist()
        source_rows = raw.count()
        bronze_uri = f"s3://{args.bucket}/{prefix}/bronze/{table}"
        # Parquet preserves JDBC Decimal and timestamp types; downstream reads
        # never requery RDS after the immutable Bronze write completes.
        raw.write.mode("errorifexists").parquet(bronze_uri)
        raw.unpersist()
        bronze = spark.read.parquet(bronze_uri)
        if bronze.count() != source_rows:
            raise ValueError(f"Bronze record count mismatch for {table}")
        clean = add_reasons(to_contract(bronze, table), table)
        rejected = clean.where(F.size("dq_reasons") > 0)
        passed = clean.where(F.size("dq_reasons") == 0).drop("dq_reasons")
        ensure_unique(passed, TABLE_KEYS[table], table)
        valid[table], rejects[table] = passed, rejected
        counts[table] = {"source": source_rows}

    # Require parent/child integrity before publishing trusted Silver tables.
    checks = [
        ("shopify_live_order_line", "shopify_live_order", ["shop_domain", "shopify_order_id"], "orphan_order_line"),
        ("shopify_live_refund", "shopify_live_order", ["shop_domain", "shopify_order_id"], "orphan_refund"),
        ("shopify_live_refund_line", "shopify_live_refund", ["shop_domain", "shopify_refund_id"], "orphan_refund_line"),
    ]
    for child, parent, keys, reason in checks:
        parent_keys = valid[parent].select(*keys).dropDuplicates()
        accepted = valid[child].join(parent_keys, on=keys, how="left_semi")
        orphan = valid[child].join(parent_keys, on=keys, how="left_anti").withColumn(
            "dq_reasons", F.array(F.lit(reason))
        )
        valid[child] = accepted
        rejects[child] = rejects[child].unionByName(orphan, allowMissingColumns=True)

    for table in TABLE_KEYS:
        accepted = valid[table].count()
        quarantined = rejects[table].count()
        if accepted + quarantined != counts[table]["source"]:
            raise ValueError(f"Reconciliation failed for {table}")
        counts[table].update({"silver": accepted, "quarantined": quarantined})
        write_partitioned(valid[table], f"s3://{args.bucket}/{prefix}/silver/{table}")
        write_partitioned(rejects[table], f"s3://{args.bucket}/{prefix}/quarantine/{table}")

    files = {layer: {table: discover_part_files(
        s3, args.bucket, f"{prefix}/{layer}/{table}") for table in TABLE_KEYS}
        for layer in ("bronze", "silver", "quarantine")}
    if any(not files["bronze"][t] for t in TABLE_KEYS):
        raise ValueError("Missing Bronze Spark output")
    if any(not files["silver"][t] and counts[t]["silver"] > 0 for t in TABLE_KEYS):
        raise ValueError("Missing expected nonempty Silver output")
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": args.batch_id,
        "source": "rds_jdbc_full_snapshot",
        "snapshot_consistency": "ingestion_barrier_required; separate_JDBC_reads",
        "counts": counts,
        "files": files,
        "published_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    # Manifest is commit marker, written only after all data quality checks pass.
    s3.put_object(Bucket=args.bucket, Key=f"{prefix}/silver_manifest.json",
                  Body=json.dumps(manifest, sort_keys=True).encode(),
                  ContentType="application/json", IfNoneMatch="*")
    print(json.dumps({"manifest": f"{prefix}/silver_manifest.json",
                      "counts": counts}, sort_keys=True))


if __name__ == "__main__":
    main()
