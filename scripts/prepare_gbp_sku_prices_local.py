"""Generate read-only GBP SKU prices from pinned historical Silver and catalog.

No Shopify, RDS or S3 writes. Run on a developer workstation with AWS S3 reads.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import boto3

from ecommerce.application.use_cases.catalog_approval import approved_catalog
from ecommerce.application.use_cases.synthetic_backfill import _eligible_source
from ecommerce.pipelines.silver.audit import read_published_silver

BUCKET = "ecommerce-demo-landing-393e7fdc5c01861ae4e5c25ad6"
SILVER_PREFIX = "historical/online_retail_ii/silver/v1"
APPROVAL_KEY = "historical/online_retail_ii/catalog_approval/v1/catalog-approval-unflagged-v1.json"
APPROVAL_SHA256 = "b348f9804564657828e55597df6cc18718933742f6d3b8a1165d2d49c18ab130"
EXPECTED_BRONZE_SHA256 = "bcbe73b35f5b7babf197fb0cb983a11f5d9ff929078d4aa53d171b1f2df2e980"


def build_manifest(tables: dict, approved: frozenset[str], *, source_sha: str) -> dict:
    if source_sha != EXPECTED_BRONZE_SHA256:
        raise ValueError("Unexpected Bronze source SHA-256")
    if len(approved) != 3408:
        raise ValueError("Approved SKU population must be exactly 3408")
    _, _, _, identities, prices = _eligible_source(tables, approved)
    if set(prices) != approved or set(identities) != approved:
        raise ValueError("Approved catalog and computed prices differ")
    rows = [
        {
            "sku": sku,
            "canonical_product_id": identities[sku][0],
            "canonical_variant_id": identities[sku][1],
            "price_gbp": f"{Decimal(prices[sku]):.2f}",
        }
        for sku in sorted(approved)
    ]
    return {
        "schema_version": "phase5-gbp-sku-prices-v1",
        "mode": "DRY_RUN",
        "currency": "GBP",
        "currency_semantics": "GBP_assumed_from_UK_source_not_verified",
        "price_method": "approved_source_sku_positive_price_median_v1",
        "source_bronze_sha256": source_sha,
        "approved_catalog_sha256": APPROVAL_SHA256,
        "count": len(rows),
        "prices": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aws-profile", default="ecommerce-admin")
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--bucket", default=BUCKET)
    parser.add_argument(
        "--output", type=Path, default=Path("reports/phase5-gbp-sku-prices-v1.json")
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists; refusing to overwrite")
    session = boto3.Session(profile_name=args.aws_profile, region_name=args.region)
    s3 = session.client("s3")
    tables, silver, bronze = read_published_silver(s3, args.bucket, SILVER_PREFIX)
    if silver.get("schema_version") != "online-retail-ii-silver-v1":
        raise ValueError("Unexpected Silver schema")
    approved = approved_catalog(s3, args.bucket, APPROVAL_KEY, APPROVAL_SHA256)
    manifest = build_manifest(tables, approved, source_sha=bronze["source_sha256"])
    raw = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(raw)
    values = [Decimal(row["price_gbp"]) for row in manifest["prices"]]
    print(json.dumps({
        "mode": "DRY_RUN", "currency": "GBP", "rows": len(values),
        "min_gbp": str(min(values)), "max_gbp": str(max(values)),
        "manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "output": str(args.output), "shopify_writes": 0,
        "rds_writes": 0, "s3_writes": 0,
    }, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
