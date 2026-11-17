"""Shopify historical sync: on-demand, read-only by default, bounded progress batches.

Run only from repository root. AWS source Silver is read-only. RDS app user
must have only required table permissions. No token is written to disk.
"""
from __future__ import annotations

import argparse
import os
from uuid import UUID

import boto3
import psycopg
from psycopg.conninfo import make_conninfo

from ecommerce.application.use_cases.shopify_customer_sync import (
    customer_batches,
    synchronize_customers,
)
from ecommerce.infrastructure.postgres.customer_identity import (
    HistoricalCustomer,
    PostgresCustomerIdentityRepository,
)
from ecommerce.infrastructure.postgres.customer_sync_ledger import PostgresCustomerSyncLedger
from ecommerce.infrastructure.shopify.client import (
    ShopifyClientCredentials,
    ShopifyConfig,
    ShopifyGraphQLClient,
)
from ecommerce.pipelines.silver.audit import read_published_silver


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--prefix", default="historical/online_retail_ii/silver/v1")
    parser.add_argument("--aws-profile", default=None, help="Optional developer CLI profile")
    parser.add_argument("--aws-region", default="eu-central-1")
    parser.add_argument("--limit", type=int, default=5,
                        help="Maximum selected customers (0-5942); explicitly set 5942 for full sync")
    parser.add_argument("--batch-size", type=int, default=100,
                        help="Progress boundary (1-1000); each customer is persisted individually")
    parser.add_argument("--start-after", default=None,
                        help="Resume after this canonical UUID (sorted ascending)")
    parser.add_argument("--allow-customer-writes", action="store_true")
    args = parser.parse_args()
    if not 0 <= args.limit <= 5942:
        parser.error("--limit must be between 0 and 5942")
    if not 1 <= args.batch_size <= 1000:
        parser.error("--batch-size must be between 1 and 1000")
    if args.start_after is not None:
        try:
            if str(UUID(args.start_after)) != args.start_after:
                raise ValueError("not canonical")
        except ValueError:
            parser.error("--start-after must be a canonical UUID")
    required = ["SHOPIFY_SHOP_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET", "DB_HOST", "DB_NAME",
                "DB_USER", "DB_PASSWORD", "DB_SSLROOTCERT"]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        parser.error("Missing environment settings: " + ", ".join(missing))
    cfg = ShopifyConfig(shop_domain=os.environ["SHOPIFY_SHOP_DOMAIN"])
    session = boto3.Session(profile_name=args.aws_profile, region_name=args.aws_region)
    s3 = session.client("s3")
    tables, _, _ = read_published_silver(s3, args.bucket, args.prefix)
    customers = tables["customers"]
    if len(customers) != 5942 or customers["customer_id"].duplicated().any():
        parser.error("Expected exactly 5,942 unique Silver customers")
    records = [HistoricalCustomer(str(row.customer_id), str(row.source_customer_id))
               for row in customers.sort_values("customer_id").itertuples(index=False)]
    if args.start_after is not None:
        records = [customer for customer in records if customer.customer_id > args.start_after]
    provider = ShopifyClientCredentials(cfg.shop_domain,
                                        os.environ["SHOPIFY_CLIENT_ID"],
                                        os.environ["SHOPIFY_CLIENT_SECRET"])
    with ShopifyGraphQLClient(cfg, token_provider=provider) as api:
        shop_gid, _ = api.verify_shop()
        api.verify_custom_id_definition()
        if not os.path.isfile(os.environ["DB_SSLROOTCERT"]):
            parser.error("DB_SSLROOTCERT must reference a readable CA bundle")
        conninfo = make_conninfo("", host=os.environ["DB_HOST"],
                                 port=os.environ.get("DB_PORT", "5432"),
                                 dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
                                 password=os.environ["DB_PASSWORD"])
        with psycopg.connect(conninfo, sslmode="verify-full",
                             sslrootcert=os.environ["DB_SSLROOTCERT"], autocommit=True) as conn:
            repo = PostgresCustomerIdentityRepository(conn, shop_gid)
            ledger = PostgresCustomerSyncLedger(conn, shop_gid)
            selection = records[:args.limit]
            total_processed = total_skipped = total_verified = 0
            print(f"Mode: {'WRITE' if args.allow_customer_writes else 'READ ONLY'}; "
                  f"selected={len(selection)}; batch_size={args.batch_size}", flush=True)
            for batch_number, batch in enumerate(customer_batches(selection, args.batch_size), start=1):
                # Stop on failure. Previous successful mappings remain durable;
                # restart from the beginning to reconcile ambiguous remote writes.
                result = synchronize_customers(batch, api, repo,
                                               allow_write=args.allow_customer_writes,
                                               ledger=ledger)
                total_processed += result.processed
                total_skipped += result.skipped_existing
                total_verified += result.verified_new
                print(f"Batch {batch_number}: processed={result.processed}, "
                      f"skipped={result.skipped_existing}, verified_new={result.verified_new}; "
                      f"total_processed={total_processed}", flush=True)
            print(f"Summary: processed={total_processed}, skipped={total_skipped}, "
                  f"verified_new={total_verified}", flush=True)
            print(f"Verified mappings in PostgreSQL: {repo.count_verified()}/5942")


if __name__ == "__main__":
    main()
