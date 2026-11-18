"""Read-only Phase 4 historical purchase-behavior calibration.

Reads checksum-verified, immutable Bronze/Silver manifests and the pinned
approved Shopify catalog. Writes only a local report; never mutates AWS.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import boto3

from ecommerce.application.use_cases.catalog_approval import approved_catalog
from ecommerce.application.use_cases.historical_calibration import (
    calibrate_historical_behavior,
    calibration_sha256,
    canonical_calibration_bytes,
)
from ecommerce.pipelines.silver.audit import read_published_silver

DEFAULT_BUCKET = "ecommerce-demo-landing-393e7fdc5c01861ae4e5c25ad6"
DEFAULT_APPROVAL_KEY = (
    "historical/online_retail_ii/catalog_approval/v1/"
    "catalog-approval-unflagged-v1.json"
)
DEFAULT_APPROVAL_SHA256 = (
    "b348f9804564657828e55597df6cc18718933742f6d3b8a1165d2d49c18ab130"
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--prefix", default="historical/online_retail_ii/silver/v1")
    parser.add_argument("--aws-profile", default="ecommerce-admin")
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--approval-key", default=DEFAULT_APPROVAL_KEY)
    parser.add_argument("--approval-sha256", default=DEFAULT_APPROVAL_SHA256)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/historical_calibration_v1.json"),
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error(f"Refusing to overwrite existing calibration report: {args.output}")
    session = boto3.Session(profile_name=args.aws_profile, region_name=args.region)
    s3 = session.client("s3")
    tables, silver_manifest, bronze_manifest = read_published_silver(
        s3, args.bucket, args.prefix
    )
    approved = approved_catalog(s3, args.bucket, args.approval_key, args.approval_sha256)
    report = calibrate_historical_behavior(
        tables,
        approved_stock_codes=frozenset(approved),
        source_sha256=bronze_manifest["source_sha256"],
    )
    report["source_silver_manifest_key"] = f"{args.prefix.strip('/')}/manifest.json"
    report["source_silver_schema_version"] = silver_manifest["schema_version"]
    report["catalog_approval_sha256"] = args.approval_sha256
    content = canonical_calibration_bytes(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as output:
        output.write(content)
    print(json.dumps({
        "saved_local_report": str(args.output),
        "report_sha256": calibration_sha256(report),
        "customers_total": report["customers_total"],
        "approved_positive_orders": report["approved_positive_orders"],
        "approved_positive_lines": report["approved_positive_lines"],
        "source_timestamp_semantics": report["source_timestamp_semantics"],
    }, sort_keys=True))


if __name__ == "__main__":
    main()
