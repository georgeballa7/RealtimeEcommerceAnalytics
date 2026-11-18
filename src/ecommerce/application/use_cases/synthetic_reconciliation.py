"""Read-only reconciliation of committed synthetic backfill data in S3.

Follow the checkpoint's manifest chain (never list uncommitted staged objects).
Exit nonzero on any integrity or financial mismatch; never write to AWS.
CLI: python scripts/reconcile_synthetic_backfill.py
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
from collections import Counter
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import boto3

from ecommerce.application.use_cases.synthetic_publisher import DEFAULT_SYNTHETIC_PREFIX

DEFAULT_BUCKET = "ecommerce-demo-landing-393e7fdc5c01861ae4e5c25ad6"
APPROVAL_KEY = (
    "historical/online_retail_ii/catalog_approval/v1/"
    "catalog-approval-unflagged-v1.json"
)
APPROVAL_SHA256 = "b348f9804564657828e55597df6cc18718933742f6d3b8a1165d2d49c18ab130"
PENNY = Decimal("0.01")


def require(ok: bool, message: str) -> None:
    if not ok:
        raise ValueError(message)


def fetch(s3: Any, bucket: str, key: str, sha256: str | None = None) -> bytes:
    response = s3.get_object(Bucket=bucket, Key=key)
    stream = response["Body"]
    try:
        raw = stream.read()
    finally:
        stream.close()
    if sha256 is not None:
        require(hashlib.sha256(raw).hexdigest() == sha256, f"SHA-256 mismatch: {key}")
    return raw


def utc(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    require(
        timestamp.tzinfo is not None and timestamp.utcoffset() == timedelta(0),
        f"Non-UTC timestamp: {value}",
    )
    return timestamp


def money(value: Any) -> Decimal:
    result = Decimal(str(value))
    require(result.is_finite() and result == result.quantize(PENNY), f"Invalid GBP amount: {value}")
    return result


def load_rows(s3: Any, bucket: str, spec: dict, expected_name: str) -> list[dict]:
    require(spec["key"].endswith("/" + expected_name + ".jsonl.gz"), "Unexpected asset name")
    raw = fetch(s3, bucket, spec["key"], spec["sha256"])
    rows = [json.loads(line) for line in gzip.decompress(raw).splitlines() if line]
    require(len(rows) == spec["rows"], f"Row count mismatch: {expected_name}")
    return rows


def reconcile(s3: Any, bucket: str, *, prefix: str = DEFAULT_SYNTHETIC_PREFIX) -> dict:
    """Validate checkpoint chain, exact object bytes, identities and GBP balances."""
    root = prefix.strip("/")
    head = json.loads(fetch(s3, bucket, f"{root}/state/head.json"))
    require(head["manifest_key"].startswith(root + "/intervals/"), "Invalid head manifest path")
    require(len(head["manifest_sha256"]) == 64, "Invalid head manifest hash")
    manifests = []
    seen = set()
    key = head["manifest_key"]
    expected_sha = head["manifest_sha256"]
    while key:
        require(key not in seen, "Manifest cycle detected")
        require(key.startswith(root + "/intervals/") and key.endswith("/manifest.json"),
                "Manifest path escaped synthetic prefix")
        seen.add(key)
        raw = fetch(s3, bucket, key, expected_sha)
        manifest = json.loads(raw)
        require(manifest["schema_version"] == "synthetic-2026-backfill-manifest-v2",
                "Unexpected synthetic manifest schema")
        manifests.append(manifest)
        key = manifest["prior_manifest_key"]
        if key:
            # Every manifest is immutable. Previous references are only keys,
            # so retrieve the prior manifest and hash its actual bytes.
            previous_raw = fetch(s3, bucket, key)
            expected_sha = hashlib.sha256(previous_raw).hexdigest()
    manifests.reverse()
    require(manifests, "Missing committed manifest")
    require(manifests[0]["start_utc"] == "2026-01-01T00:00:00Z", "Incorrect initial start")
    for index, manifest in enumerate(manifests):
        start, cutoff = utc(manifest["start_utc"]), utc(manifest["cutoff_utc"])
        require(start < cutoff, "Invalid half-open interval")
        if index:
            require(manifests[index - 1]["cutoff_utc"] == manifest["start_utc"],
                    "Gap or overlap in committed intervals")
            require(manifests[index - 1]["lineage"] == manifest["lineage"],
                    "Lineage drift between intervals")
    latest = manifests[-1]
    require(latest["start_utc"] == head["start_utc"], "Head start mismatch")
    require(latest["cutoff_utc"] == head["cutoff_utc"], "Head cutoff mismatch")
    require(all(head[name] == value for name, value in latest["lineage"].items()),
            "Head lineage mismatch")
    require(latest["lineage"]["approved_catalog_sha256"] == APPROVAL_SHA256,
            "Unexpected catalog lineage")
    approved = json.loads(fetch(s3, bucket, APPROVAL_KEY, APPROVAL_SHA256))
    require(approved["schema_version"] == "catalog-approval-v1", "Catalog schema mismatch")
    allowed = set(approved["approved_stock_codes"])
    require(len(allowed) == len(approved["approved_stock_codes"]), "Duplicate approved stock code")

    all_orders = {}
    all_items = {}
    all_cancellations = {}
    order_sums = Counter()
    totals = {"gross_gbp": Decimal("0.00"), "cancelled_gbp": Decimal("0.00")}
    counts = Counter()
    for manifest in manifests:
        start, cutoff = utc(manifest["start_utc"]), utc(manifest["cutoff_utc"])
        specs = manifest["files"]
        require(set(specs) == {"orders", "order_items", "cancellations"},
                "Incomplete S3 asset set")
        orders = load_rows(s3, bucket, specs["orders"], "orders")
        items = load_rows(s3, bucket, specs["order_items"], "order_items")
        cancellations = load_rows(s3, bucket, specs["cancellations"], "cancellations")

        for order in orders:
            oid = order["order_id"]
            require(oid and oid not in all_orders, f"Duplicate order: {oid}")
            require(start <= utc(order["event_time_utc"]) < cutoff, "Order timestamp out of range")
            require(order["source_system"] == "synthetic_2026" and order["synthetic"] is True,
                    "Incorrect order provenance")
            require(order["currency"] == "GBP" and bool(order["customer_id"]),
                    "Invalid order currency or customer identity")
            # Published v2 stores its GBP assumption on items; older order/event
            # records may not repeat the metadata. Reject conflicting values.
            require(order.get("currency_assumption", "GBP_assumed_from_UK_source_not_verified")
                    == "GBP_assumed_from_UK_source_not_verified",
                    "Conflicting GBP assumption on order")
            all_orders[oid] = order
            totals["gross_gbp"] += money(order["order_total"])

        for item in items:
            iid = item["order_item_id"]
            require(iid and iid not in all_items, f"Duplicate order item: {iid}")
            require(item["order_id"] in all_orders, "Orphan order item")
            require(start <= utc(item["event_time_utc"]) < cutoff, "Item timestamp out of range")
            require(item["event_time_utc"] == all_orders[item["order_id"]]["event_time_utc"],
                    "Item/order timestamp mismatch")
            require(item["source_stock_code"] in allowed, "Order item uses unapproved SKU")
            require(item["currency"] == "GBP" and item["synthetic"] is True,
                    "Incorrect item provenance/currency")
            require(item["currency_assumption"] == "GBP_assumed_from_UK_source_not_verified",
                    "Missing GBP assumption on item")
            require(bool(item["product_id"]) and bool(item["variant_id"]), "Missing product identity")
            require(type(item["quantity"]) is int and item["quantity"] > 0,
                    "Invalid item quantity")
            require(money(item["line_amount"]) == money(item["unit_price"]) * item["quantity"],
                    "GBP line amount mismatch")
            order_sums[item["order_id"]] += money(item["line_amount"])
            all_items[iid] = item

        for event in cancellations:
            eid = event["event_id"]
            require(eid and eid not in all_cancellations, "Duplicate cancellation event ID")
            require(start <= utc(event["event_time_utc"]) < cutoff,
                    "Cancellation timestamp out of range")
            require(event["order_id"] in all_orders and event["order_item_id"] in all_items,
                    "Cancellation references unknown order or item")
            order = all_orders[event["order_id"]]
            item = all_items[event["order_item_id"]]
            require(item["order_id"] == event["order_id"], "Cancellation/item order mismatch")
            require(event["customer_id"] == order["customer_id"], "Cancellation customer mismatch")
            require(event["original_order_time_utc"] == order["event_time_utc"],
                    "Cancellation original timestamp mismatch")
            require(utc(order["event_time_utc"]) < utc(event["event_time_utc"]),
                    "Cancellation not after purchase")
            require(event["source_stock_code"] == item["source_stock_code"]
                    and event["product_id"] == item["product_id"]
                    and event["variant_id"] == item["variant_id"],
                    "Cancellation product mismatch")
            require(event["currency"] == "GBP" and event["synthetic"] is True,
                    "Incorrect cancellation provenance")
            require(event.get("currency_assumption", "GBP_assumed_from_UK_source_not_verified")
                    == "GBP_assumed_from_UK_source_not_verified",
                    "Conflicting GBP assumption on cancellation")
            require(event["event_type"] == "order_item_cancelled"
                    and event["provenance"] == "simulated_cancellation_not_verified_refund",
                    "Unexpected cancellation semantics")
            require(type(event["cancelled_quantity"]) is int
                    and 0 < event["cancelled_quantity"] <= item["quantity"],
                    "Invalid cancelled quantity")
            require(money(event["unit_price"]) == money(item["unit_price"]),
                    "Cancellation unit price mismatch")
            amount = money(event["cancelled_amount"])
            require(amount == money(item["unit_price"]) * event["cancelled_quantity"],
                    "Cancellation GBP amount mismatch")
            totals["cancelled_gbp"] += amount
            all_cancellations[eid] = event

        counts.update({"orders": len(orders), "order_items": len(items),
                       "cancellations": len(cancellations)})
    for oid, order in all_orders.items():
        require(order_sums[oid] == money(order["order_total"]),
                f"Order total does not reconcile: {oid}")
    require(totals["cancelled_gbp"] <= totals["gross_gbp"],
            "Cancelled merchandise exceeds gross merchandise")
    return {
        "status": "PASS",
        "committed_intervals": len(manifests),
        "start_utc": manifests[0]["start_utc"],
        "cutoff_utc": latest["cutoff_utc"],
        "orders": counts["orders"],
        "order_items": counts["order_items"],
        "cancellations": counts["cancellations"],
        "approved_catalog_size": len(allowed),
        "gross_gbp": str(totals["gross_gbp"]),
        "cancelled_merchandise_gbp": str(totals["cancelled_gbp"]),
        "net_after_cancellations_gbp": str(totals["gross_gbp"] - totals["cancelled_gbp"]),
        "note": "Synthetic cancelled merchandise value is not a verified refund or settlement.",
        "policy_sha256": latest["lineage"]["policy_sha256"],
        "calibration_sha256": latest["lineage"]["calibration_sha256"],
        "aws_writes": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--aws-profile", default="ecommerce-admin")
    parser.add_argument("--region", default="eu-central-1")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    args = parser.parse_args()
    s3 = boto3.Session(
        profile_name=args.aws_profile, region_name=args.region
    ).client("s3")
    try:
        result = reconcile(s3, args.bucket)
    except Exception as exc:
        result = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}",
                  "aws_writes": False}
        print(json.dumps(result, indent=2, sort_keys=True))
        raise SystemExit(1) from exc
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
