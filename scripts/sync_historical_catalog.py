"""Reusable, on-demand historical Shopify catalog reconciliation ECS entrypoint.

Read-only by default. In write mode, require an immutable approved stock-code
manifest verified by SHA-256; no blanket write authorization from item_class.
The canonical Silver object population remains unchanged on S3.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections.abc import Iterable

import boto3
import psycopg
from psycopg.conninfo import make_conninfo

from ecommerce.application.use_cases.catalog_selection import (
    CatalogCandidate,
    select_catalog_candidates,
)
from ecommerce.application.use_cases.catalog_sync import reconcile_catalog
from ecommerce.infrastructure.postgres.catalog_identity import (
    PostgresCatalogIdentityRepository,
)
from ecommerce.infrastructure.postgres.catalog_lease import PostgresCatalogLease
from ecommerce.infrastructure.shopify.catalog_client import ShopifyCatalogClient
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


def approved_catalog(s3, bucket: str, key: str, checksum: str) -> frozenset[str]:
    """Load explicit approved SKU list from S3, reject mutable/unverified data."""
    if not key.startswith("historical/online_retail_ii/catalog_approval/v1/"):
        raise ValueError("Catalog approvals must use the dedicated S3 prefix")
    if len(checksum) != 64 or any(x not in "0123456789abcdef" for x in checksum):
        raise ValueError("Expected a lowercase SHA-256 approval checksum")
    stream = s3.get_object(Bucket=bucket, Key=key)["Body"]
    try:
        body = stream.read()
    finally:
        stream.close()
    if hashlib.sha256(body).hexdigest() != checksum:
        raise ValueError("Catalog approval manifest checksum mismatch")
    manifest = json.loads(body)
    if not isinstance(manifest, dict) or manifest.get("schema_version") != "catalog-approval-v1":
        raise ValueError("Unexpected catalog approval schema")
    items = manifest.get("approved_stock_codes")
    if not isinstance(items, list) or not items:
        raise ValueError("Approved stock-code list is empty")
    if not all(isinstance(item, str) and item.strip() == item and item for item in items):
        raise ValueError("Invalid approved stock code")
    if len(set(items)) != len(items):
        raise ValueError("Duplicate approved stock codes")
    return frozenset(items)


def selected_catalog(
    candidates: Iterable[CatalogCandidate], *, limit: int,
    allow_write: bool, approved: frozenset[str] | None = None,
) -> tuple[CatalogCandidate, ...]:
    if limit < 0:
        raise ValueError("Catalog limit must be non-negative")
    candidates = tuple(candidates)
    if allow_write:
        if not approved:
            raise ValueError("Write-enabled sync requires reviewed catalog approvals")
        known = {item.source_stock_code for item in candidates}
        if not approved.issubset(known):
            raise ValueError("Catalog approvals reference unknown or ineligible codes")
        candidates = tuple(c for c in candidates if c.source_stock_code in approved)
    return candidates[:limit]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bucket", required=True)
    p.add_argument("--prefix", default="historical/online_retail_ii/silver/v1")
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--aws-region", default="eu-central-1")
    p.add_argument("--aws-profile", default=None)
    p.add_argument("--allow-catalog-writes", action="store_true")
    p.add_argument("--approval-key", default=None)
    p.add_argument("--approval-sha256", default=None)
    return p


def main() -> None:
    p = parser()
    args = p.parse_args()
    if args.limit < 0:
        p.error("Limit must be non-negative")
    missing = [key for key in REQUIRED_ENV if not os.environ.get(key)]
    if missing:
        p.error("Missing environment settings: " + ", ".join(missing))
    if not os.path.isfile(os.environ["DB_SSLROOTCERT"]):
        p.error("DB_SSLROOTCERT must reference a readable CA bundle")
    if args.allow_catalog_writes and (not args.approval_key or not args.approval_sha256):
        p.error("Catalog writes require both --approval-key and --approval-sha256")

    session = boto3.Session(profile_name=args.aws_profile, region_name=args.aws_region)
    s3 = session.client("s3")
    tables, _, _ = read_published_silver(s3, args.bucket, args.prefix)
    selection = select_catalog_candidates(tables["products"], tables["product_variants"])
    approved = (
        approved_catalog(s3, args.bucket, args.approval_key, args.approval_sha256)
        if args.allow_catalog_writes else None
    )
    candidates = selected_catalog(
        selection.candidates,
        limit=args.limit,
        allow_write=args.allow_catalog_writes,
        approved=approved,
    )
    print(
        "Catalog selection: "
        + json.dumps(
            {
                "source_products": selection.product_count,
                "source_variants": selection.variant_count,
                "merchandise_candidates": len(selection.candidates),
                "excluded_by_class": selection.excluded_by_class,
                "selected": len(candidates),
                "write_authorized": args.allow_catalog_writes,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    cfg = ShopifyConfig(shop_domain=os.environ["SHOPIFY_SHOP_DOMAIN"])
    provider = ShopifyClientCredentials(
        cfg.shop_domain,
        os.environ["SHOPIFY_CLIENT_ID"], os.environ["SHOPIFY_CLIENT_SECRET"],
    )
    with ShopifyGraphQLClient(cfg, token_provider=provider) as graphql:
        shop_gid, _ = graphql.verify_shop()
        api = ShopifyCatalogClient(graphql)
        api.verify_product_definition()
        conninfo = make_conninfo(
            "", host=os.environ["DB_HOST"],
            port=os.environ.get("DB_PORT", "5432"),
            dbname=os.environ["DB_NAME"],
            user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        )
        with psycopg.connect(
            conninfo, sslmode="verify-full",
            sslrootcert=os.environ["DB_SSLROOTCERT"], autocommit=True,
        ) as connection:
            repository = PostgresCatalogIdentityRepository(connection, shop_gid)
            lease = PostgresCatalogLease(connection, shop_gid)
            summary = reconcile_catalog(
                candidates, api, repository,
                allow_write=args.allow_catalog_writes,
                lease=lease if args.allow_catalog_writes else None,
            )
            db_products, db_variants = repository.count_verified()
            print(
                "Catalog reconciliation: "
                + json.dumps(
                    {
                        "inspected": summary.inspected,
                        "product_mappings_verified": summary.product_mappings_verified,
                        "variant_mappings_verified": summary.variant_mappings_verified,
                        "product_creates_requested": summary.product_creates_requested,
                        "variant_creates_requested": summary.variant_creates_requested,
                        "missing_products": summary.missing_products,
                        "missing_variants": summary.missing_variants,
                        "rds_verified_products": db_products,
                        "rds_verified_variants": db_variants,
                    },
                    sort_keys=True,
                ),
                flush=True,
            )


if __name__ == "__main__":
    main()
