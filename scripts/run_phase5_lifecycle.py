"""On-demand Shopify synthetic order cancellation / explicit refund executor.

Default: PREVIEW. No payments/refunds are initiated without explicit request.
Uses existing successful generation checkpoints, not newly generated orders.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ecommerce.application.use_cases.shopify_order_lifecycle import (
    ORDER_QUERY, Operation, advance, cancellation_plan,
)
from ecommerce.infrastructure.aws.lifecycle_journal import S3LifecycleJournal
from ecommerce.infrastructure.shopify.client import ShopifyConfig, ShopifyClientCredentials, ShopifyGraphQLClient


def completed_generation_records(s3, bucket: str, *, run_ids: list[str]):
    """Enumerate known run identifiers; don't rely on volatile local state."""
    for run_id in run_ids:
        prefix = f"simulation/phase5/live/v1/{run_id}/"
        paginator = s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                key = obj["Key"]
                if not key.endswith(".json"):
                    continue
                record = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
                if record.get("run_id") != run_id or record.get("status") != "complete":
                    continue
                oid = record.get("order_id")
                if isinstance(oid, str) and oid.startswith("gid://shopify/Order/"):
                    yield run_id, oid


def recent_generation_run_ids(s3, bucket: str, now: datetime, lookback_days: int = 16) -> list[str]:
    """Recover eligible run IDs after restarts; no manual list required daily."""
    import re
    cutoff = now - timedelta(days=lookback_days)
    runs = set()
    paginator = s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix="simulation/phase5/live/v1/"):
        for obj in page.get("Contents", []):
            if obj["LastModified"] < cutoff:
                continue
            parts = obj["Key"].split("/")
            if len(parts) == 6 and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,63}", parts[4]):
                runs.add(parts[4])
    return sorted(runs)


def eligible_cancellations(gql, records, *, now, policy):
    for run_id, order_id in records:
        details = gql.query(ORDER_QUERY, {"id": order_id}).get("order")
        if not details or details.get("id") != order_id:
            raise RuntimeError("Completed generation order not found in Shopify")
        # Shopify is authoritative for order creation, not a local clock.
        created = datetime.fromisoformat(details["createdAt"].replace("Z", "+00:00"))
        plan = cancellation_plan(
            run_id, order_id, created, seed=policy["seed"],
            probability_percent=policy["cancellation_probability_percent"],
            max_delay_days=policy["cancellation_delay_max_days"])
        if plan["selected"] and datetime.fromisoformat(plan["due_at"]) <= now:
            yield Operation(run_id, order_id, "cancel", plan["due_at"])


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", action="append",
                    help="Known generator run ID, can repeat for previous 14 days")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--confirm-apply", action="store_true")
    ap.add_argument("--refund-order", help="Existing Shopify order GID for explicit refund")
    ap.add_argument("--refund-line", help="Existing Shopify line-item GID")
    ap.add_argument("--refund-quantity", type=int)
    ap.add_argument("--limit", type=int, default=100)
    args = ap.parse_args()
    if args.apply != args.confirm_apply:
        ap.error("Applying mutations requires --apply --confirm-apply")
    if not 1 <= args.limit <= 100:
        ap.error("Limit must be between 1 and 100")
    if bool(args.refund_order) != bool(args.refund_line):
        ap.error("Refund order and line must both be supplied")
    if args.refund_order and not args.run_id:
        ap.error("Explicit refund requires its originating generator run ID")
    if args.refund_order and (not args.refund_quantity or args.refund_quantity < 1):
        ap.error("Explicit refund requires a positive quantity")
    for name in ("SHOPIFY_SHOP_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET", "S3_LANDING_BUCKET"):
        if not os.environ.get(name): ap.error(f"Missing {name}")
    if os.environ["SHOPIFY_SHOP_DOMAIN"] != "realtime-ecommerce-analytics.myshopify.com":
        ap.error("Unexpected Shopify shop")
    import boto3
    s3 = boto3.client("s3")
    bucket = os.environ["S3_LANDING_BUCKET"]
    policy_path = Path(__file__).resolve().parents[1] / "config/phase4-simulation-policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    provider = ShopifyClientCredentials(os.environ["SHOPIFY_SHOP_DOMAIN"],
                                        os.environ["SHOPIFY_CLIENT_ID"], os.environ["SHOPIFY_CLIENT_SECRET"])
    journal = S3LifecycleJournal(s3, bucket)
    with ShopifyGraphQLClient(ShopifyConfig(shop_domain=os.environ["SHOPIFY_SHOP_DOMAIN"]),
                               token_provider=provider) as gql:
        gql.verify_shop()
        if args.refund_order:
            # Refunds are never part of the automatic 10%-cancellation policy.
            # They require an explicit order, item and quantity.
            operations = [Operation(args.run_id[0], args.refund_order, "refund",
                                    now.isoformat(), args.refund_line, args.refund_quantity)]
        else:
            ids = args.run_id or recent_generation_run_ids(s3, bucket, now)
            gen_records = completed_generation_records(s3, bucket, run_ids=ids)
            operations = eligible_cancellations(gql, gen_records, now=now, policy=policy)
        stats = {"mode": "APPLY" if args.apply else "PREVIEW", "seen": 0, "outcomes": {}}
        for o in operations:
            if stats["seen"] >= args.limit: break
            result = advance(gql, journal, o, now=now, allow_mutations=args.apply)
            stats["seen"] += 1
            stats["outcomes"][result] = stats["outcomes"].get(result, 0) + 1
        print(json.dumps(stats, sort_keys=True))

if __name__ == "__main__":
    main()
