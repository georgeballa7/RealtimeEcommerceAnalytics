"""Generate reproducible 2026 synthetic orders; publish to S3 only if explicitly enabled.

The invocation-time UTC cutoff is captured ONCE before reading sources.
Default is read-only dry-run. No RDS or Shopify writes.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import fields
from datetime import UTC, datetime
from pathlib import Path

import boto3
from botocore.exceptions import ClientError

from ecommerce.application.use_cases.catalog_approval import approved_catalog
from ecommerce.application.use_cases.historical_calibration import (
    calibrate_historical_behavior,
    calibration_sha256,
)
from ecommerce.application.use_cases.synthetic_backfill import (
    START_UTC,
    SimulationPolicy,
    generate_interval,
)
from ecommerce.application.use_cases.synthetic_publisher import (
    DEFAULT_SYNTHETIC_PREFIX,
    publish_synthetic_interval,
)
from ecommerce.pipelines.silver.audit import read_published_silver

BUCKET = "ecommerce-demo-landing-393e7fdc5c01861ae4e5c25ad6"
APPROVAL_KEY = (
    "historical/online_retail_ii/catalog_approval/v1/"
    "catalog-approval-unflagged-v1.json"
)
APPROVAL_SHA256 = (
    "b348f9804564657828e55597df6cc18718933742f6d3b8a1165d2d49c18ab130"
)
SILVER_PREFIX = "historical/online_retail_ii/silver/v1"


def _read_checkpoint(s3, bucket: str) -> dict | None:
    key = f"{DEFAULT_SYNTHETIC_PREFIX}/state/head.json"
    try:
        response = s3.get_object(Bucket=bucket, Key=key)
    except ClientError as error:
        if error.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    body = response["Body"]
    try:
        return json.loads(body.read())
    finally:
        body.close()


def main() -> None:
    # This capture is the exact end-exclusive boundary even if S3 reads are slow.
    invocation_cutoff_utc = datetime.now(UTC)

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--policy", type=Path, default=Path("config/phase4-simulation-policy.json")
    )
    parser.add_argument("--aws-profile", default="ecommerce-admin")
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument(
        "--execute-s3", action="store_true",
        help="Allow immutable synthetic S3 objects and conditional checkpoint advancement",
    )
    args = parser.parse_args()

    config = json.loads(args.policy.read_text(encoding="utf-8"))
    expected = {field.name for field in fields(SimulationPolicy)}
    if set(config) != expected:
        raise ValueError(f"Simulation config keys differ from pinned contract: {set(config) ^ expected}")
    policy = SimulationPolicy(**config)
    session = boto3.Session(profile_name=args.aws_profile, region_name=args.region)
    s3 = session.client("s3")
    checkpoint = _read_checkpoint(s3, args.bucket)
    start_utc = (
        datetime.fromisoformat(checkpoint["cutoff_utc"].replace("Z", "+00:00"))
        if checkpoint else START_UTC
    )
    if start_utc >= invocation_cutoff_utc:
        raise ValueError("Backfill checkpoint already reaches or exceeds invocation UTC cutoff")

    tables, silver, bronze = read_published_silver(s3, args.bucket, SILVER_PREFIX)
    approved = approved_catalog(s3, args.bucket, APPROVAL_KEY, APPROVAL_SHA256)
    calibration = calibrate_historical_behavior(
        tables,
        approved_stock_codes=approved,
        source_sha256=bronze["source_sha256"],
    )
    # Enforce exact pinned source and catalog population from our verified audit.
    if len(tables["customers"]) != 5942 or len(approved) != 3408:
        raise ValueError("Unexpected historical customer/catalog population")
    if silver["schema_version"] != "online-retail-ii-silver-v1":
        raise ValueError("Unexpected Silver version")

    orders, items = generate_interval(
        tables, calibration, approved,
        start_utc=start_utc,
        cutoff_utc=invocation_cutoff_utc,
        policy=policy,
    )
    result = {
        "mode": "execute_s3" if args.execute_s3 else "dry_run",
        "start_utc": start_utc.isoformat(),
        "cutoff_utc": invocation_cutoff_utc.isoformat(),
        "source_system": "synthetic_2026",
        "policy_sha256": policy.fingerprint,
        "calibration_sha256": calibration_sha256(calibration),
        "orders": len(orders),
        "order_items": len(items),
        "approved_stock_codes": len(approved),
        "published": False,
    }
    if args.execute_s3:
        manifest = publish_synthetic_interval(
            s3, bucket=args.bucket,
            start_utc=start_utc, cutoff_utc=invocation_cutoff_utc,
            orders=orders, items=items,
            policy_sha256=policy.fingerprint,
            calibration_sha256=calibration_sha256(calibration),
            source_bronze_sha256=bronze["source_sha256"],
            approved_catalog_sha256=APPROVAL_SHA256,
        )
        result["published"] = True
        result["manifest"] = manifest
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
