"""RDS-independent Shopify webhook ingestion through SQS.

SQS delivers at least once; S3 landing deduplicates by shop and webhook ID.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_SQS_MESSAGE_BYTES = 1_048_576


class WebhookQueueError(RuntimeError):
    """SQS could not durably acknowledge a webhook."""


@dataclass(frozen=True)
class IncomingWebhook:
    shop_domain: str
    webhook_id: str
    event_id: str | None
    topic: str
    payload: dict[str, Any]


class SqsWebhookQueue:
    def __init__(self, client: Any, queue_url: str):
        if not queue_url.startswith("https://"):
            raise ValueError("EVENT_QUEUE_URL must be HTTPS")
        self.client = client
        self.queue_url = queue_url

    def accept(self, event: IncomingWebhook) -> bool:
        envelope = {
            "schema_version": 1,
            "source": "shopify_webhook",
            "shop_domain": event.shop_domain,
            "webhook_id": event.webhook_id,
            "event_id": event.event_id,
            "topic": event.topic,
            "payload": event.payload,
        }
        body = json.dumps(envelope, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        if len(body.encode("utf-8")) > MAX_SQS_MESSAGE_BYTES:
            raise ValueError("Webhook envelope exceeds SQS message limit")
        try:
            response = self.client.send_message(
                QueueUrl=self.queue_url,
                MessageBody=body,
                MessageAttributes={"topic": {"DataType": "String", "StringValue": event.topic}},
            )
        except Exception as exc:
            raise WebhookQueueError("SQS publish unavailable") from exc
        if not response.get("MessageId"):
            raise WebhookQueueError("SQS did not acknowledge the webhook")
        return True
