"""Transform immutable AWS Bronze into canonical, customer-linked AWS Silver."""
import argparse
import json
from pathlib import Path

import boto3

from ecommerce.pipelines.historical_bronze_s3 import terraform_landing_bucket
from ecommerce.pipelines.historical_silver import (
    DEFAULT_BRONZE_PREFIX, DEFAULT_SILVER_PREFIX, publish_silver,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", help="Defaults to deployed Terraform landing bucket")
    parser.add_argument("--terraform-dir", type=Path, default=Path("infrastructure/terraform"))
    parser.add_argument("--profile", default="ecommerce-admin")
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--bronze-prefix", default=DEFAULT_BRONZE_PREFIX)
    parser.add_argument("--silver-prefix", default=DEFAULT_SILVER_PREFIX)
    args = parser.parse_args()
    bucket = args.bucket or terraform_landing_bucket(args.terraform_dir)
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    manifest = publish_silver(session.client("s3"), bucket, args.bronze_prefix, args.silver_prefix)
    print(json.dumps(manifest["reconciliation"], indent=2))
    print(f"Silver manifest: s3://{bucket}/{args.silver_prefix.strip('/')}/manifest.json")


if __name__ == "__main__":
    main()
