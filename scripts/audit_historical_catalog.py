"""Read-only historical catalog eligibility review against verified published S3.

Reports are generated locally (or printed), never uploaded or authorized for
Shopify writes. Operational evidence and review decisions belong in Notion.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import boto3

from ecommerce.application.use_cases.catalog_review import catalog_review
from ecommerce.pipelines.silver.audit import read_published_silver
from ecommerce.pipelines.silver.historical import load_verified_bronze


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--silver-prefix", default="historical/online_retail_ii/silver/v1")
    parser.add_argument("--bronze-prefix", default="historical/online_retail_ii/bronze/v1")
    parser.add_argument("--aws-profile", default=None)
    parser.add_argument("--aws-region", default="eu-central-1")
    parser.add_argument("--output", type=Path, help="Optional local JSON review file")
    args = parser.parse_args()

    session = boto3.Session(profile_name=args.aws_profile, region_name=args.aws_region)
    client = session.client("s3")
    silver, silver_manifest, bronze_manifest = read_published_silver(
        client, args.bucket, args.silver_prefix
    )
    bronze, verified_bronze_manifest = load_verified_bronze(
        client, args.bucket, args.bronze_prefix
    )
    if (
        silver_manifest["source_bronze_manifest_key"]
        != f"{args.bronze_prefix.strip('/')}/manifest.json"
        or silver_manifest["source_bronze_sha256"]
        != verified_bronze_manifest["source_sha256"]
        or bronze_manifest["source_sha256"]
        != verified_bronze_manifest["source_sha256"]
    ):
        raise ValueError("The published Silver/Bronze lineage does not match")

    review = catalog_review(silver, bronze)
    report = {
        "source": {
            "bucket": args.bucket,
            "silver_manifest": f"{args.silver_prefix.strip('/')}/manifest.json",
            "bronze_manifest": f"{args.bronze_prefix.strip('/')}/manifest.json",
            "bronze_source_sha256": verified_bronze_manifest["source_sha256"],
        },
        **review,
    }
    # The safe summary is sufficient for CloudWatch or a status update.
    summary = {key: value for key, value in report.items() if key != "review_queue"}
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)
    if args.output:
        if args.output.exists():
            raise FileExistsError("Refusing to overwrite an existing catalog report")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
        print(f"Local review report: {args.output}", flush=True)


if __name__ == "__main__":
    main()
