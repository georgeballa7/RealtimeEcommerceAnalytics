"""DynamoDB index of landed Shopify webhook objects.

The S3 object is the source of truth; this index is a recoverable processing queue.
SQS Lambda must index every successfully landed object BEFORE acknowledging SQS.
"""
from __future__ import annotations

from datetime import datetime, timezone


def index_landed_event(table, *, shop_domain: str, webhook_id: str, s3_key: str,
                       bucket: str, checksum: str, observed_at: datetime | None = None) -> bool:
    """Insert idempotently; return False for an identical already-indexed event.

    A conflict raises (and must cause SQS redelivery), not silently overwrites.
    """
    import botocore.exceptions

    now = observed_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("Observed timestamp must be timezone-aware")
    identity = f"{shop_domain}\x00{webhook_id}"
    payload = {"event_key": identity, "state": "pending", "bucket": bucket,
               "s3_key": s3_key, "checksum": checksum, "observed_at": now.isoformat()}
    try:
        table.put_item(Item=payload, ConditionExpression="attribute_not_exists(event_key)")
        return True
    except botocore.exceptions.ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    prior = table.get_item(Key={"event_key": identity}, ConsistentRead=True).get("Item")
    if not prior or any(prior.get(field) != payload[field]
                        for field in ("bucket", "s3_key", "checksum")):
        raise RuntimeError("Conflicting Shopify webhook identity in pending-event index")
    return False


def claim_pending_events(table, *, limit: int = 25):
    """GSI query may be eventually consistent; ledger handles repeated delivery."""
    if not 1 <= limit <= 100:
        raise ValueError("Invalid batch size")
    response = table.query(
        IndexName="by_state", KeyConditionExpression="#s = :pending",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":pending": "pending"}, Limit=limit,
    )
    return response.get("Items", [])


def mark_processed(table, item):
    table.update_item(
        Key={"event_key": item["event_key"]},
        UpdateExpression="SET #s = :done",
        ConditionExpression="#s = :pending AND checksum = :checksum",
        ExpressionAttributeNames={"#s": "state"},
        ExpressionAttributeValues={":pending": "pending", ":done": "processed",
                                   ":checksum": item["checksum"]},
    )


def signal_activity(control_table, observed_at: datetime | None = None):
    """Refresh durable activity after landing; clears any previous idle timer."""
    now = observed_at or datetime.now(timezone.utc)
    control_table.update_item(
        Key={"id": "phase5-controller"},
        UpdateExpression="SET last_event_at=:now REMOVE idle_since",
        ExpressionAttributeValues={":now": now.isoformat()},
    )
