"""On-demand Shopify synthetic order cancellation / explicit refund executor.

Default: PREVIEW. No payments/refunds are initiated without explicit request.
Uses existing successful generation checkpoints, not newly generated orders.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from pathlib import Path

from ecommerce.application.use_cases.shopify_order_lifecycle import (
    ORDER_QUERY, Operation, advance, cancellation_plan, synthetic_refund_plan,
    selected_for_simulation, stable_key, require_nonfinancial_simulation,
    LifecycleError,
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


def eligible_cancellations(gql, records, *, now, policy, journal=None):
    for run_id, order_id in records:
        if not selected_for_simulation(
                run_id, order_id, "cancel", seed=policy["seed"],
                probability_percent=policy["cancellation_probability_percent"]):
            continue
        if journal and (journal.get(stable_key(run_id, order_id, "cancel")) or {}).get("state") == "complete":
            continue
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



def eligible_synthetic_refunds(gql, records, *, now, policy, refund_policy, journal=None):
    """One stable, nonfinancial refund candidate per verified synthetic order.

    Skip candidates selected for cancellation. No real-payment refund may enter
    the scheduled workflow. Explicit operational refunds remain separate.
    """
    for run_id, order_id in records:
        if not selected_for_simulation(
                run_id, order_id, "refund", seed=policy["seed"],
                probability_percent=refund_policy["probability_percent"]):
            continue
        if journal and (journal.get(stable_key(run_id, order_id, "refund")) or {}).get("state") == "complete":
            continue
        order = gql.query(ORDER_QUERY, {"id": order_id}).get("order")
        if not order or order.get("id") != order_id:
            raise RuntimeError("Synthetic source order missing in Shopify")
        created = datetime.fromisoformat(order["createdAt"].replace("Z", "+00:00"))
        cancelled = cancellation_plan(
            run_id, order_id, created, seed=policy["seed"],
            probability_percent=policy["cancellation_probability_percent"],
            max_delay_days=policy["cancellation_delay_max_days"])
        if cancelled["selected"] or order.get("cancelledAt"):
            continue
        plan = synthetic_refund_plan(
            run_id, order_id, created, seed=policy["seed"],
            probability_percent=refund_policy["probability_percent"],
            max_delay_days=refund_policy["max_delay_days"])
        if not plan["selected"] or datetime.fromisoformat(plan["due_at"]) > now:
            continue
        # Accept a Shopify manual transaction only for exact generator provenance.
        try:
            require_nonfinancial_simulation(order, run_id=run_id)
        except LifecycleError:
            continue
        lines = order.get("lineItems") or {}
        if (lines.get("pageInfo") or {}).get("hasNextPage"):
            raise RuntimeError("Refund candidate has uninspected Shopify lines")
        candidates = sorted(
            (line["id"] for line in lines.get("nodes", [])
             if isinstance(line.get("id"), str)
             and isinstance(line.get("refundableQuantity"), int)
             and line["refundableQuantity"] >= 1))
        if candidates:
            yield Operation(run_id, order_id, "refund", plan["due_at"],
                            candidates[0], 1, synthetic_nonfinancial=True)



def publish_nightly_lifecycle(s3, bucket: str, day: str, stats: dict) -> None:
    """Publish last, after cancellation/refund mutations have settled.

    Both Shopify generation slots must have committed independently first.
    The resulting marker permits a complete post-22:00 RDS/Glue snapshot.
    """
    from botocore.exceptions import ClientError
    for slot in ("morning", "afternoon"):
        run_id = f"live-{day.replace('-', '')}-{slot}"
        key = f"simulation/phase5/slots/v1/{run_id}.json"
        observed = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        if observed != {"schema_version": 1, "run_id": run_id,
                        "slot": slot, "generation": "complete"}:
            raise RuntimeError(f"Incomplete or invalid generation marker: {run_id}")
    if stats.get("mode") != "APPLY":
        raise RuntimeError("Nightly completion requires applied lifecycle actions")
    key = f"simulation/phase5/slots/v1/lifecycle-{day}.json"
    marker = {"schema_version": 1, "day": day, "lifecycle": "settled",
              "generation_slots": ["morning", "afternoon"]}
    try:
        s3.put_object(Bucket=bucket, Key=key, IfNoneMatch="*",
                      Body=json.dumps(marker, sort_keys=True).encode(),
                      ContentType="application/json")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"PreconditionFailed", "412"}:
            raise
        existing = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        if existing != marker:
            raise RuntimeError("Conflicting lifecycle commit marker") from exc

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
    ap.add_argument("--commit-nightly", action="store_true",
                    help="Commit the 22:00 lifecycle only after settled operations")
    ap.add_argument("--require-settled", action="store_true",
                    help="Fail closed unless every selected lifecycle operation settles")
    args = ap.parse_args()
    if args.commit_nightly and (not args.apply or not args.require_settled or args.refund_order):
        ap.error("Nightly lifecycle requires --apply --confirm-apply --require-settled without explicit refund")
    if args.apply != args.confirm_apply:
        ap.error("Applying mutations requires --apply --confirm-apply")
    if not 1 <= args.limit <= (500 if args.require_settled else 100):
        ap.error("Limit must be 1..100 (standalone) or 1..500 (combined slot)")
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
    refund_policy = json.loads((policy_path.parent / "phase5-synthetic-refund-policy.json").read_text(encoding="utf-8"))
    if (refund_policy["version"] != "phase5-synthetic-refund-policy-v2"
            or refund_policy["mode"] != "development_store_synthetic_manual_or_test_only"
            or refund_policy["currency"] != "GBP"
            or refund_policy["allow_test_transactions"] is not True
            or refund_policy["allow_verified_synthetic_manual_transactions"] is not True
            or refund_policy["allowed_synthetic_manual_gateway"] != "manual"
            or refund_policy["require_generator_note_and_tags"] is not True
            or refund_policy["block_non_test_transactions"] is not True
            or refund_policy["refund_quantity_per_order"] != 1
            or refund_policy["restock_type"] != "NO_RESTOCK"):
        raise RuntimeError("Unreviewed or unsafe automatic synthetic refund policy")
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
            generation = list(gen_records)
            operations = list(eligible_cancellations(gql, generation, now=now, policy=policy, journal=journal))
            operations.extend(eligible_synthetic_refunds(
                gql, generation, now=now, policy=policy, refund_policy=refund_policy, journal=journal))
            # A backlog of previously completed operations must not starve new
            # refunds/cancellations under the existing per-run limit.
            operations = [op for op in operations
                          if (journal.get(op.key) or {}).get("state") != "complete"]
            operations.sort(key=lambda op: (op.requested_at, op.key))
        stats = {"mode": "APPLY" if args.apply else "PREVIEW", "seen": 0, "outcomes": {}}
        if args.require_settled and (not args.apply or len(operations) > args.limit):
            raise RuntimeError("Scheduled lifecycle cannot certify an incomplete or preview batch")
        pending = []
        for o in operations:
            if stats["seen"] >= args.limit:
                break
            result = advance(gql, journal, o, now=now, allow_mutations=args.apply)
            stats["seen"] += 1
            stats["outcomes"][result] = stats["outcomes"].get(result, 0) + 1
            if result in {"pending", "awaiting_shopify_state"}:
                pending.append(o)
        # Shopify cancellation is asynchronous. Never publish a finished slot
        # merely because its mutation job was accepted. Poll idempotently.
        if args.require_settled:
            for _ in range(12):
                if not pending:
                    break
                time.sleep(20)
                remaining = []
                for o in pending:
                    result = advance(
                        gql, journal, o, now=datetime.now(timezone.utc),
                        allow_mutations=True)
                    stats["outcomes"][result] = stats["outcomes"].get(result, 0) + 1
                    if result in {"pending", "awaiting_shopify_state"}:
                        remaining.append(o)
                pending = remaining
            if pending:
                raise RuntimeError(
                    f"Scheduled lifecycle still has {len(pending)} unsettled Shopify operations")
        if args.commit_nightly:
            local_day = now.astimezone(ZoneInfo("Europe/Berlin")).date().isoformat()
            publish_nightly_lifecycle(s3, bucket, local_day, stats)
        print(json.dumps(stats, sort_keys=True))

if __name__ == "__main__":
    main()
