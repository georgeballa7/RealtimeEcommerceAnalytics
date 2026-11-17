"""Shopify historical customer sync: read-only by default, explicit small-batch writes.

Run only from repository root. AWS source Silver is read-only. RDS app user
must have only required table permissions. No token is written to disk.
"""
from __future__ import annotations

import argparse
import os

import boto3
import psycopg

from ecommerce.application.use_cases.shopify_customer_sync import synchronize_customers
from ecommerce.infrastructure.postgres.customer_identity import (
    HistoricalCustomer, PostgresCustomerIdentityRepository,
)
from ecommerce.infrastructure.shopify.client import ShopifyConfig, ShopifyGraphQLClient
from ecommerce.pipelines.silver.audit import read_published_silver


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", default="historical/online_retail_ii/silver/v1")
    parser.add_argument("--aws-profile", default="ecommerce-admin")
    parser.add_argument("--aws-region", default="eu-central-1")
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--allow-customer-writes", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.limit <= 5 and args.allow_customer_writes:
        parser.error("Write-mode limit is capped at five per invocation")
    if args.limit < 0:
        parser.error("Limit cannot be negative")
    required = ["SHOPIFY_SHOP_DOMAIN", "SHOPIFY_ADMIN_TOKEN", "ECOMMERCE_DATABASE_URL"]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        parser.error("Missing environment settings: " + ", ".join(missing))
    cfg = ShopifyConfig(shop_domain=os.environ["SHOPIFY_SHOP_DOMAIN"],
                        token=os.environ["SHOPIFY_ADMIN_TOKEN"])
    session = boto3.Session(profile_name=args.aws_profile, region_name=args.aws_region)
    s3 = session.client("s3")
    tables, _, _ = read_published_silver(s3, args.bucket, args.prefix)
    customers = tables["customers"]
    if len(customers) != 5942 or customers["customer_id"].duplicated().any():
        parser.error("Expected exactly 5,942 unique Silver customers")
    records = [HistoricalCustomer(str(row.customer_id), str(row.source_customer_id))
               for row in customers.sort_values("customer_id").itertuples(index=False)]
    with ShopifyGraphQLClient(cfg) as api:
        shop_gid, _ = api.verify_shop()
        api.verify_custom_id_definition()
        with psycopg.connect(os.environ["ECOMMERCE_DATABASE_URL"], sslmode="verify-full") as conn:
            repo = PostgresCustomerIdentityRepository(conn, shop_gid)
            result = synchronize_customers(records, api, repo,
                                           allow_write=args.allow_customer_writes,
                                           limit=args.limit)
            print(result)
            print(f"Verified mappings in PostgreSQL: {repo.count_verified()}/5942")


if __name__ == "__main__":
    main()
