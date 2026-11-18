"""On-demand Shopify customer reconciliation ECS entrypoint.

CLI and AWS/RDS/Shopify wiring only; business selection and reporting live in
application.use_cases.customer_sync_runner. Read-only is the default.
"""
from __future__ import annotations

import argparse
import os

import boto3
import psycopg
from psycopg.conninfo import make_conninfo

from ecommerce.application.use_cases.customer_sync_runner import (
    CustomerRunOptions,
    run_customer_sync,
    select_silver_customers,
)
from ecommerce.infrastructure.postgres.customer_identity import PostgresCustomerIdentityRepository
from ecommerce.infrastructure.postgres.customer_sync_ledger import PostgresCustomerSyncLedger
from ecommerce.infrastructure.shopify.client import (
    ShopifyClientCredentials,
    ShopifyConfig,
    ShopifyGraphQLClient,
)
from ecommerce.pipelines.silver.audit import read_published_silver


REQUIRED_ENV = (
    "SHOPIFY_SHOP_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET",
    "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSLROOTCERT",
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", default="historical/online_retail_ii/silver/v1")
    parser.add_argument("--aws-profile", default=None, help="Optional developer CLI profile")
    parser.add_argument("--aws-region", default="eu-central-1")
    parser.add_argument("--limit", type=int, default=5, help="Maximum selected customers")
    parser.add_argument(
        "--batch-size", type=int, default=100,
        help="Progress boundary (1-1000); each customer persists individually",
    )
    parser.add_argument("--start-after", default=None, help="Canonical UUID cursor")
    parser.add_argument("--allow-customer-writes", action="store_true")
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        options = CustomerRunOptions(
            limit=args.limit,
            batch_size=args.batch_size,
            start_after=args.start_after,
            allow_write=args.allow_customer_writes,
        )
    except ValueError as exc:
        parser.error(str(exc))
    missing = [key for key in REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        parser.error("Missing environment settings: " + ", ".join(missing))
    rootcert = os.environ["DB_SSLROOTCERT"]
    if not os.path.isfile(rootcert):
        parser.error("DB_SSLROOTCERT must reference a readable CA bundle")

    shop = ShopifyConfig(shop_domain=os.environ["SHOPIFY_SHOP_DOMAIN"])
    session = boto3.Session(profile_name=args.aws_profile, region_name=args.aws_region)
    tables, _, _ = read_published_silver(session.client("s3"), args.bucket, args.prefix)
    customers = tables["customers"]
    try:
        selection = select_silver_customers(customers, options)
    except ValueError as exc:
        parser.error(str(exc))

    provider = ShopifyClientCredentials(
        shop.shop_domain,
        os.environ["SHOPIFY_CLIENT_ID"],
        os.environ["SHOPIFY_CLIENT_SECRET"],
    )
    with ShopifyGraphQLClient(shop, token_provider=provider) as api:
        shop_gid, _ = api.verify_shop()
        api.verify_custom_id_definition()
        conninfo = make_conninfo(
            "", host=os.environ["DB_HOST"],
            port=os.environ.get("DB_PORT", "5432"),
            dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
            password=os.environ["DB_PASSWORD"],
        )
        with psycopg.connect(
            conninfo, sslmode="verify-full", sslrootcert=rootcert, autocommit=True
        ) as conn:
            repository = PostgresCustomerIdentityRepository(conn, shop_gid)
            ledger = PostgresCustomerSyncLedger(conn, shop_gid)
            run_customer_sync(
                selection,
                source_population=len(customers),
                options=options,
                api=api,
                repository=repository,
                ledger=ledger,
                emit=lambda line: print(line, flush=True),
            )


if __name__ == "__main__":
    main()
