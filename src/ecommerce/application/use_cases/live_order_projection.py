"""Idempotent, source-timestamp-aware Shopify operational SQL projection.

Runs only when RDS is available; each envelope and its derived rows commit in
one transaction. Never mutates immutable historical datasets.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation

ORDER_TOPICS = {"orders/create", "orders/updated", "orders/cancelled", "orders/paid", "orders/fulfilled"}
REFUND_TOPICS = {"refunds/create"}


class InvalidShopifyEvent(ValueError):
    pass


def positive_id(value, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise InvalidShopifyEvent(f"Missing or invalid {name}")
    text = str(value)
    if not text.isdecimal() or int(text) <= 0:
        raise InvalidShopifyEvent(f"Missing or invalid {name}")
    return int(text)


def timestamp(value, name: str) -> datetime:
    if not isinstance(value, str):
        raise InvalidShopifyEvent(f"Missing {name}")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidShopifyEvent(f"Invalid {name}") from exc
    if result.tzinfo is None:
        raise InvalidShopifyEvent(f"Timezone required for {name}")
    return result


def money(value, name: str) -> Decimal:
    try:
        amount = Decimal(str(value))
    except (TypeError, InvalidOperation) as exc:
        raise InvalidShopifyEvent(f"Invalid {name}") from exc
    if not amount.is_finite() or amount < 0 or amount.as_tuple().exponent < -2:
        raise InvalidShopifyEvent(f"Invalid {name}")
    return amount


def order_details(payload: dict) -> dict:
    order_id = positive_id(payload.get("id"), "order id")
    if payload.get("currency") != "GBP":
        raise InvalidShopifyEvent("Unexpected currency")
    created = timestamp(payload.get("created_at"), "created_at")
    updated = timestamp(payload.get("updated_at"), "updated_at")
    if updated < created:
        raise InvalidShopifyEvent("Order update predates creation")
    customer = payload.get("customer") or {}
    customer_id = customer.get("id")
    if customer_id is not None:
        customer_id = f"gid://shopify/Customer/{positive_id(customer_id, 'customer id')}"
    cancelled = payload.get("cancelled_at")
    return {
        "id": order_id, "customer": customer_id, "created": created, "updated": updated,
        "cancelled": timestamp(cancelled, "cancelled_at") if cancelled else None,
        "gross": money(payload.get("total_price"), "total_price"),
        "current": money(payload.get("current_total_price", payload.get("total_price")), "current_total_price"),
        "financial": payload.get("financial_status"),
        "fulfillment": payload.get("fulfillment_status"),
        "synthetic": "ecommerce-synthetic" in {x.strip() for x in (payload.get("tags") or "").split(",")},
    }


def apply_envelope(connection, envelope: dict, s3_key: str, checksum: str) -> str:
    """Apply webhook exactly once per (shop, webhook ID), with atomic ledger.

    Call with a psycopg connection. The caller owns connection creation and
    ensures only one instance of this worker runs for the target DB.
    """
    if envelope.get("schema_version") != 1 or envelope.get("source") != "shopify_webhook":
        raise InvalidShopifyEvent("Unsupported event format")
    shop, webhook, topic = (envelope.get(k) for k in ("shop_domain", "webhook_id", "topic"))
    if not isinstance(shop, str) or not shop.endswith(".myshopify.com"):
        raise InvalidShopifyEvent("Invalid shop")
    if not isinstance(webhook, str) or not webhook:
        raise InvalidShopifyEvent("Invalid webhook ID")
    if topic not in ORDER_TOPICS | REFUND_TOPICS:
        raise InvalidShopifyEvent("Unsupported topic")
    payload = envelope.get("payload")
    if not isinstance(payload, dict):
        raise InvalidShopifyEvent("Missing payload")
    if hashlib.sha256(json.dumps(envelope, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest() != checksum:
        raise InvalidShopifyEvent("S3 event checksum mismatch")
    with connection.transaction():
        with connection.cursor() as cursor:
            cursor.execute("""
                INSERT INTO shopify_live_event_ledger
                  (shop_domain,webhook_id,topic,source_event_id,s3_key,object_sha256)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (shop_domain,webhook_id) DO NOTHING
                RETURNING webhook_id
            """, (shop, webhook, topic, envelope.get("event_id"), s3_key, checksum))
            if cursor.fetchone() is None:
                cursor.execute("""SELECT object_sha256 FROM shopify_live_event_ledger
                                  WHERE shop_domain=%s AND webhook_id=%s""", (shop, webhook))
                row = cursor.fetchone()
                if row is None or row[0] != checksum:
                    raise InvalidShopifyEvent("Conflicting replay payload")
                return "duplicate"
            if topic in ORDER_TOPICS:
                info = order_details(payload)
                cursor.execute("""
                    INSERT INTO shopify_live_order
                      (shop_domain,shopify_order_id,shopify_customer_gid,currency,
                       created_at_source,updated_at_source,cancelled_at,financial_status,
                       fulfillment_status,gross_total,current_total,synthetic)
                    VALUES (%s,%s,%s,'GBP',%s,%s,%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (shop_domain,shopify_order_id) DO UPDATE SET
                      shopify_customer_gid=EXCLUDED.shopify_customer_gid,
                      updated_at_source=EXCLUDED.updated_at_source,
                      cancelled_at=EXCLUDED.cancelled_at,
                      financial_status=EXCLUDED.financial_status,
                      fulfillment_status=EXCLUDED.fulfillment_status,
                      gross_total=EXCLUDED.gross_total,
                      current_total=EXCLUDED.current_total,
                      synthetic=EXCLUDED.synthetic
                    WHERE shopify_live_order.updated_at_source <= EXCLUDED.updated_at_source
                    RETURNING shopify_order_id
                """, (shop, info["id"], info["customer"], info["created"], info["updated"],
                      info["cancelled"], info["financial"], info["fulfillment"],
                      info["gross"], info["current"], info["synthetic"]))
                applied = cursor.fetchone() is not None
                if applied:
                    for line in payload.get("line_items", []):
                        lid = positive_id(line.get("id"), "line id")
                        variant = line.get("variant_id")
                        variant = positive_id(variant,"variant id") if variant is not None else None
                        quantity = line.get("quantity")
                        if isinstance(quantity,bool) or not isinstance(quantity,int) or quantity < 0:
                            raise InvalidShopifyEvent("Invalid line quantity")
                        cursor.execute("""
                            INSERT INTO shopify_live_order_line
                              (shop_domain,shopify_order_id,shopify_line_id,shopify_variant_id,
                               sku,quantity,unit_price,discount_total,updated_at_source)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            ON CONFLICT (shop_domain,shopify_order_id,shopify_line_id) DO UPDATE SET
                              shopify_variant_id=EXCLUDED.shopify_variant_id,
                              sku=EXCLUDED.sku,quantity=EXCLUDED.quantity,
                              unit_price=EXCLUDED.unit_price,discount_total=EXCLUDED.discount_total,
                              updated_at_source=EXCLUDED.updated_at_source
                            WHERE shopify_live_order_line.updated_at_source <= EXCLUDED.updated_at_source
                        """, (shop, info["id"], lid, variant, line.get("sku"), quantity,
                              money(line.get("price"),"line price"),
                              money(line.get("total_discount","0"),"total_discount"), info["updated"]))
                return "applied" if applied else "stale"
            rid = positive_id(payload.get("id"), "refund id")
            oid = positive_id(payload.get("order_id"), "refund order id")
            created = timestamp(payload.get("created_at"), "refund created_at")
            # These are item subtotals, NOT actual payment refunds.
            # A refund webhook can arrive before its order on standard SQS.
            # Preserve the record; reconciliation view identifies missing orders.
            lines = payload.get("refund_line_items") or []
            total = sum((money(x.get("subtotal", "0"),"refund subtotal") for x in lines), Decimal("0"))
            digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            cursor.execute("""
                INSERT INTO shopify_live_refund
                  (shop_domain,shopify_refund_id,shopify_order_id,created_at_source,refund_line_subtotal,payload_sha256)
                VALUES (%s,%s,%s,%s,%s,%s)
                ON CONFLICT (shop_domain,shopify_refund_id) DO NOTHING
            """, (shop,rid,oid,created,total,digest))
            for line in lines:
                refund_qty = line.get("quantity", 0)
                if isinstance(refund_qty, bool) or not isinstance(refund_qty, int) or refund_qty < 0:
                    raise InvalidShopifyEvent("Invalid refund line quantity")
                cursor.execute("""
                    INSERT INTO shopify_live_refund_line
                      (shop_domain,shopify_refund_id,shopify_refund_line_id,shopify_order_line_id,
                       quantity,subtotal)
                    VALUES (%s,%s,%s,%s,%s,%s)
                    ON CONFLICT (shop_domain,shopify_refund_id,shopify_refund_line_id) DO NOTHING
                """, (shop,rid,positive_id(line.get("id"),"refund line id"),
                      positive_id(line["line_item_id"],"original order line") if line.get("line_item_id") is not None else None,
                      refund_qty,money(line.get("subtotal","0"),"refund subtotal")))
            return "applied"
