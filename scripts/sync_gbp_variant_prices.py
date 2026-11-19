"""Guarded Phase 5 GBP reconciliation. Dry-run by default.

Run from an approved private-network task with Shopify OAuth + RDS TLS env vars.
Migration 007 must be applied separately first. No implicit migrations, no S3 writes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg.conninfo import make_conninfo

from ecommerce.application.use_cases.shopify_price_reconciliation import reconcile_targets
from ecommerce.infrastructure.postgres.variant_pricing import PostgresVariantPricingRepository
from ecommerce.infrastructure.shopify.client import (
    ShopifyClientCredentials, ShopifyConfig, ShopifyGraphQLClient,
)
from ecommerce.infrastructure.shopify.variant_pricing import ShopifyVariantPricing

EXPECTED_SHA256 = "7850c9f5faaea304b0d42c468a10179c8d44905f5ca82b7767e7497152fb2c06"
REQUIRED_ENV = (
    "SHOPIFY_SHOP_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET",
    "DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "DB_SSLROOTCERT",
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path,
                        default=Path("config/catalog/phase5-gbp-sku-prices-v1.json"))
    parser.add_argument("--limit", type=int, default=3408)
    parser.add_argument("--apply", action="store_true", help="Authorize Shopify and RDS writes")
    parser.add_argument("--confirm-sha256", default=None)
    parser.add_argument("--expected-shop", default="realtime-ecommerce-analytics.myshopify.com")
    args = parser.parse_args()
    if not 1 <= args.limit <= 3408:
        parser.error("--limit must be between 1 and 3408")
    if args.apply and args.confirm_sha256 != EXPECTED_SHA256:
        parser.error("--apply requires exact --confirm-sha256 matching approved manifest")
    missing = [key for key in REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        parser.error("Missing environment settings: " + ", ".join(missing))
    if not Path(os.environ["DB_SSLROOTCERT"]).is_file():
        parser.error("DB_SSLROOTCERT must point to a readable CA bundle")
    if os.environ["SHOPIFY_SHOP_DOMAIN"] != args.expected_shop:
        parser.error("Unexpected Shopify shop domain")
    raw = args.manifest.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != EXPECTED_SHA256:
        parser.error("Manifest checksum differs from approved Phase 5 release")
    manifest = json.loads(raw)
    cfg = ShopifyConfig(shop_domain=args.expected_shop)
    provider = ShopifyClientCredentials(
        cfg.shop_domain, os.environ["SHOPIFY_CLIENT_ID"], os.environ["SHOPIFY_CLIENT_SECRET"])
    with ShopifyGraphQLClient(cfg, token_provider=provider) as gql:
        shop_gid, _ = gql.verify_shop()
        currency = (gql.query("query Phase5Currency { shop { currencyCode } }").get("shop") or {}).get("currencyCode")
        if currency != "GBP":
            raise ValueError("Shop currency changed from GBP")
        dsn = make_conninfo("", host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
                            dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
                            password=os.environ["DB_PASSWORD"])
        with psycopg.connect(dsn, sslmode="verify-full",
                             sslrootcert=os.environ["DB_SSLROOTCERT"], autocommit=True) as connection:
            repo = PostgresVariantPricingRepository(connection, shop_gid)
            plan = reconcile_targets(manifest, repo.verified_variants())
            api = ShopifyVariantPricing(gql)
            summary = {"mode": "APPLY" if args.apply else "DRY_RUN", "inspected": 0,
                       "already_matching": 0, "requires_update": 0, "verified_updated": 0,
                       "rds_recorded": 0, "manifest_sha256": digest}
            for target in plan[:args.limit]:
                actual = api.read(sku=target.sku, product_gid=target.product_gid,
                                  variant_gid=target.variant_gid)
                summary["inspected"] += 1
                if actual == target.price:
                    summary["already_matching"] += 1
                else:
                    summary["requires_update"] += 1
                    if args.apply:
                        api.update(sku=target.sku, product_gid=target.product_gid,
                                   variant_gid=target.variant_gid, price=target.price)
                        actual = api.read(sku=target.sku, product_gid=target.product_gid,
                                          variant_gid=target.variant_gid)
                        if actual != target.price:
                            raise RuntimeError("Shopify read-back price differs; stopping")
                        summary["verified_updated"] += 1
                if args.apply:
                    # A successful independent Shopify GET is the only source for RDS writes.
                    repo.save_confirmed(variant_id=target.variant_id, price_gbp=actual,
                                        manifest_sha256=digest)
                    summary["rds_recorded"] += 1
                if summary["inspected"] % 100 == 0 or summary["inspected"] == args.limit:
                    print("Phase 5 progress: " + json.dumps(summary, sort_keys=True), flush=True)
            print("Phase 5 final: " + json.dumps(summary, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
