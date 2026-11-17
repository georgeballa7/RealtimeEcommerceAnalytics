"""Publish the Online Retail II Bronze dataset to the deployed AWS S3 landing bucket."""

import argparse
from pathlib import Path

from ecommerce.pipelines.historical_bronze_s3 import (
    DEFAULT_PREFIX,
    terraform_landing_bucket,
    upload_verified_bronze,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Publish verified Online Retail II Bronze data to S3")
    parser.add_argument("--source", type=Path, default=Path("data/raw/online_retail_II.xlsx"))
    parser.add_argument("--bucket", help="Override the deployed Terraform landing_bucket_name")
    parser.add_argument("--terraform-dir", type=Path, default=Path("infrastructure/terraform"))
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument("--aws-profile", default="ecommerce-admin")
    parser.add_argument("--region", default="eu-central-1")
    args = parser.parse_args()
    bucket = args.bucket or terraform_landing_bucket(args.terraform_dir)
    published = upload_verified_bronze(
        args.source, bucket=bucket, prefix=args.prefix,
        aws_profile=args.aws_profile, region=args.region,
    )
    print(f"Published and verified {published['total_rows']:,} Bronze lines")
    print(f"S3 dataset: s3://{bucket}/{args.prefix.strip('/')}/")
    print(f"Manifest: s3://{bucket}/{args.prefix.strip('/')}/manifest.json")


if __name__ == "__main__":
    main()
