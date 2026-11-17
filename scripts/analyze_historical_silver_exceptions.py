"""Read-only historical Silver exception review; save findings locally."""
import argparse
import json
from pathlib import Path

import boto3

from ecommerce.pipelines.bronze.s3 import terraform_landing_bucket
from ecommerce.pipelines.silver.exceptions import analyze_published_silver


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", help="Defaults to Terraform landing_bucket_name")
    parser.add_argument("--terraform-dir", type=Path, default=Path("infrastructure/terraform"))
    parser.add_argument("--profile", default="ecommerce-admin")
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--silver-prefix", default="historical/online_retail_ii/silver/v1")
    parser.add_argument("--sample-limit", type=int, default=100)
    parser.add_argument("--output", type=Path, default=Path("reports/historical_silver_exceptions_v1.json"))
    args = parser.parse_args()
    bucket = args.bucket or terraform_landing_bucket(args.terraform_dir)
    s3 = boto3.Session(profile_name=args.profile, region_name=args.region).client("s3")
    report = analyze_published_silver(s3, bucket, args.silver_prefix,
                                      sample_limit=args.sample_limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"Saved local exception report: {args.output}")


if __name__ == "__main__":
    main()
