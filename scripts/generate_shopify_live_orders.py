"""Guarded Phase 5 live Shopify order generator.

Creates actual Shopify orders from existing synced variants/customers through
draftOrderCreate + draftOrderComplete. State is checkpointed to versioned S3.
Default is PREVIEW (no mutation or S3 changes).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from ecommerce.infrastructure.shopify.client import (
    ShopifyClientCredentials, ShopifyConfig, ShopifyGraphQLClient,
)

PRODUCTS = """query GeneratorProducts($after: String) {
  products(first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { id status variants(first: 100) { nodes { id sku price } pageInfo { hasNextPage } } }
  }
}"""
CUSTOMERS = """query GeneratorCustomers($after: String) {
  customers(first: 100, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes { id }
  }
}"""
CREATE = """mutation GeneratorCreate($input: DraftOrderInput!) {
  draftOrderCreate(input: $input) {
    draftOrder { id status }
    userErrors { code field }
  }
}"""
COMPLETE = """mutation GeneratorComplete($id: ID!) {
  draftOrderComplete(id: $id) {
    draftOrder { id status order { id } }
    userErrors { field message }
  }
}"""
READ_DRAFT = """query GeneratorDraft($id: ID!) {
  draftOrder(id: $id) { id status order { id } }
}"""
APPROVED_MANIFEST_SHA = "7850c9f5faaea304b0d42c468a10179c8d44905f5ca82b7767e7497152fb2c06"


def paginate(gql, query: str, field: str) -> list[dict]:
    results = []
    cursor = None
    for _ in range(120):
        page = gql.query(query, {"after": cursor})[field]
        results.extend(page["nodes"])
        info = page["pageInfo"]
        if not info["hasNextPage"]:
            return results
        next_cursor = info["endCursor"]
        if not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor:
            raise RuntimeError("Shopify pagination failed closed")
        cursor = next_cursor
    raise RuntimeError("Shopify result exceeds configured pagination safety cap")


def approved_variants(products: list[dict], manifest: dict) -> list[dict]:
    expected = {p["sku"]: Decimal(p["price_gbp"]) for p in manifest["prices"]}
    variants = {}
    for product in products:
        for node in product["variants"]["nodes"]:
            sku = node.get("sku")
            if sku not in expected:
                continue
            if sku in variants:
                raise RuntimeError("Duplicate approved SKU found in live Shopify")
            if product["variants"]["pageInfo"]["hasNextPage"]:
                raise RuntimeError("Product has uninspected variants")
            if Decimal(node["price"]) != expected[sku]:
                raise RuntimeError("Approved Shopify price drift detected")
            variants[sku] = {"id": node["id"], "sku": sku, "price": node["price"]}
    if len(variants) != manifest["count"] or set(variants) != set(expected):
        raise RuntimeError("Approved Shopify SKU population differs from manifest")
    return [variants[k] for k in sorted(variants)]


def make_orders(variants: list[dict], customers: list[str], run_id: str,
                count: int, seed: int, policy: dict) -> list[dict]:
    if not customers or not variants:
        raise RuntimeError("Missing approved Shopify customers or products")
    rng = random.Random(int.from_bytes(
        hashlib.sha256(f"{seed}:{run_id}".encode()).digest()[:8], "big"))
    orders = []
    for i in range(count):
        wholesale = rng.randint(1, 100) > policy["retail_probability_percent"]
        line_cap = policy["wholesale_line_cap"] if wholesale else policy["retail_line_cap"]
        unit_cap = (policy["wholesale_units_per_line_cap"] if wholesale
                    else policy["retail_units_per_line_cap"])
        line_count = rng.randint(1, min(3, line_cap, len(variants)))
        chosen = rng.sample(variants, line_count)
        lines = [{"variantId": v["id"], "quantity": rng.randint(1, min(3, unit_cap))}
                 for v in chosen]
        orders.append({
            "index": i,
            "customer_id": rng.choice(customers),
            "lines": lines,
            "segment": "wholesale" if wholesale else "retail",
        })
    return orders


class S3OperationJournal:
    def __init__(self, client, bucket: str, run_id: str):
        self.client, self.bucket, self.run_id = client, bucket, run_id

    def key(self, index: int) -> str:
        return f"simulation/phase5/live/v1/{self.run_id}/{index:05d}.json"

    def read(self, index: int) -> dict | None:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self.key(index))
        except self.client.exceptions.NoSuchKey:
            return None
        return json.loads(obj["Body"].read())

    def save(self, index: int, record: dict, *, create: bool = False) -> None:
        kwargs = {"Bucket": self.bucket, "Key": self.key(index),
                  "Body": json.dumps(record, sort_keys=True).encode(),
                  "ContentType": "application/json"}
        if create:
            kwargs["IfNoneMatch"] = "*"
        self.client.put_object(**kwargs)


def check_errors(payload: dict, operation: str) -> dict:
    if not isinstance(payload, dict) or payload.get("userErrors"):
        raise RuntimeError(f"Shopify {operation} rejected; inspect draft in Admin")
    draft = payload.get("draftOrder")
    if not isinstance(draft, dict) or not str(draft.get("id", "")).startswith("gid://shopify/DraftOrder/"):
        raise RuntimeError(f"Shopify {operation} returned invalid draft identity")
    return draft


def continue_one(gql, journal, record, order, run_id, *, freshly_reserved=False):
    i = order["index"]
    if record["status"] == "reserved":
        if not freshly_reserved:
            raise RuntimeError(f"Operation {i} reserved with unknown Shopify outcome; reconcile manually")
        op_tag = hashlib.sha256(f"{run_id}:{i}".encode()).hexdigest()[:16]
        payload = {"purchasingEntity": {"customerId": order["customer_id"]},
                   "lineItems": order["lines"],
                   "tags": ["ecommerce-synthetic", "phase5-live", f"phase5-{op_tag}"],
                   "note": f"SYNTHETIC TEST ORDER; phase5 operation {run_id}/{i}",
                   "presentmentCurrencyCode": "GBP",
                   "visibleToCustomer": False}
        draft = check_errors(gql.query(CREATE, {"input": payload}, mutation=True)["draftOrderCreate"],
                             "draft creation")
        record.update(status="draft", draft_id=draft["id"])
        journal.save(i, record)
    if record["status"] == "draft":
        existing = gql.query(READ_DRAFT, {"id": record["draft_id"]}).get("draftOrder")
        if not existing or existing["id"] != record["draft_id"]:
            raise RuntimeError("Draft identity disappeared; stopping")
        if (existing.get("order") or {}).get("id"):
            completed = existing
        elif existing["status"] == "COMPLETED":
            raise RuntimeError("Completed draft without an order ID; reconcile manually")
        else:
            completed = check_errors(
                gql.query(COMPLETE, {"id": record["draft_id"]}, mutation=True)["draftOrderComplete"],
                "draft completion")
        order_id = (completed.get("order") or {}).get("id")
        if not isinstance(order_id, str) or not order_id.startswith("gid://shopify/Order/"):
            raise RuntimeError("Draft completion outcome ambiguous; check Shopify before retry")
        record.update(status="complete", order_id=order_id)
        journal.save(i, record)
        return "created"
    raise RuntimeError(f"Invalid operation state: {record['status']}")


def process_one(gql, journal, order, run_id):
    i = order["index"]
    expected = hashlib.sha256(json.dumps(order, sort_keys=True).encode()).hexdigest()
    record = journal.read(i)
    if record is None:
        record = {"run_id": run_id, "index": i, "status": "reserved",
                  "order_sha256": expected, "draft_id": None, "order_id": None}
        journal.save(i, record, create=True)
        fresh = True
    else:
        fresh = False
    if record.get("order_sha256") != expected:
        raise RuntimeError(f"Operation {i}: deterministic plan changed; refusing replay")
    if record.get("status") == "complete":
        return "already_complete"
    return continue_one(gql, journal, record, order, run_id, freshly_reserved=fresh)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--count", type=int, default=45)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-run-id")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,63}", args.run_id):
        parser.error("Invalid run ID (3-64 letters/numbers/underscore/hyphen)")
    if not 1 <= args.count <= 100:
        parser.error("Count must be 1..100")
    if args.apply and args.confirm_run_id != args.run_id:
        parser.error("Write mode requires exact --confirm-run-id")
    for env in ("SHOPIFY_SHOP_DOMAIN", "SHOPIFY_CLIENT_ID", "SHOPIFY_CLIENT_SECRET"):
        if not os.environ.get(env):
            parser.error(f"Missing {env}")
    if os.environ["SHOPIFY_SHOP_DOMAIN"] != "realtime-ecommerce-analytics.myshopify.com":
        parser.error("Unexpected Shopify shop")
    root = Path(__file__).resolve().parents[1]
    manifest_bytes = (root / "config/catalog/phase5-gbp-sku-prices-v1.json").read_bytes()
    if hashlib.sha256(manifest_bytes).hexdigest() != APPROVED_MANIFEST_SHA:
        parser.error("Approved manifest checksum mismatch")
    manifest = json.loads(manifest_bytes)
    policy = json.loads((root / "config/phase4-simulation-policy.json").read_text())
    if policy["currency"] != "GBP":
        parser.error("Unexpected policy currency")
    provider = ShopifyClientCredentials(
        os.environ["SHOPIFY_SHOP_DOMAIN"], os.environ["SHOPIFY_CLIENT_ID"],
        os.environ["SHOPIFY_CLIENT_SECRET"])
    with ShopifyGraphQLClient(ShopifyConfig(shop_domain=os.environ["SHOPIFY_SHOP_DOMAIN"]),
                               token_provider=provider) as gql:
        gql.verify_shop()
        products = paginate(gql, PRODUCTS, "products")
        variants = approved_variants(products, manifest)
        customers = [c["id"] for c in paginate(gql, CUSTOMERS, "customers")
                     if str(c.get("id", "")).startswith("gid://shopify/Customer/")]
        if len(customers) < 5000:
            raise RuntimeError("Synced historical customer population is unexpectedly small")
        orders = make_orders(variants, sorted(set(customers)), args.run_id, args.count,
                             policy["seed"], policy)
        summary = {"run_id": args.run_id, "mode": "APPLY" if args.apply else "PREVIEW",
                   "planned": len(orders), "approved_variants": len(variants),
                   "available_customers": len(customers),
                   "created": 0, "already_complete": 0}
        if not args.apply:
            print(json.dumps(summary, sort_keys=True), flush=True)
            return
        if not os.environ.get("S3_LANDING_BUCKET"):
            parser.error("S3_LANDING_BUCKET required for durable mutation ledger")
        import boto3
        journal = S3OperationJournal(boto3.client("s3"), os.environ["S3_LANDING_BUCKET"], args.run_id)
        for order in orders:
            outcome = process_one(gql, journal, order, args.run_id)
            summary[outcome] += 1
            print(json.dumps(summary, sort_keys=True), flush=True)
        print("PHASE5_LIVE_GENERATOR_COMPLETE " + json.dumps(summary, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
