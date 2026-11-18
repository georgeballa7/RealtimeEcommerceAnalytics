"""At-least-once SQS to durable, idempotent S3 webhook event landing.

Conditional S3 writes prevent overwrites; replay is acknowledged only if the
existing object's content hash matches. Never log raw customer payloads.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass

from botocore.exceptions import ClientError

LOGGER = logging.getLogger(__name__)
MAX_MESSAGE_BYTES = 1_048_576


class InvalidEvent(ValueError):
    """Unsupported or malformed event; SQS retries then DLQ."""


class ConflictingReplay(RuntimeError):
    """Same webhook ID with conflicting contents."""


@dataclass(frozen=True)
class WebhookEnvelope:
    shop_domain: str
    webhook_id: str
    topic: str
    event_id: str | None
    payload: dict

    @classmethod
    def from_sqs_body(cls, text: str) -> "WebhookEnvelope":
        if not isinstance(text, str) or len(text.encode("utf-8")) > MAX_MESSAGE_BYTES:
            raise InvalidEvent("Invalid message size")
        try:
            body = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise InvalidEvent("Invalid JSON envelope") from exc
        if not isinstance(body, dict):
            raise InvalidEvent("Envelope must be a JSON object")
        if body.get("schema_version") != 1 or body.get("source") != "shopify_webhook":
            raise InvalidEvent("Unsupported envelope version or source")
        domain = body.get("shop_domain")
        webhook_id = body.get("webhook_id")
        topic = body.get("topic")
        event_id = body.get("event_id")
        payload = body.get("payload")
        if (
            not isinstance(domain, str)
            or domain != domain.lower()
            or len(domain) > 253
            or not domain.endswith(".myshopify.com")
            or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-." for c in domain)
            or domain.startswith(".")
            or ".." in domain
        ):
            raise InvalidEvent("Invalid shop domain")
        if (
            not isinstance(webhook_id, str)
            or not 0 < len(webhook_id) <= 200
            or not isinstance(topic, str)
            or not 0 < len(topic) <= 200
            or (event_id is not None and not isinstance(event_id, str))
            or not isinstance(payload, dict)
        ):
            raise InvalidEvent("Invalid webhook fields")
        return cls(domain, webhook_id, topic, event_id, payload)

    def object_key(self) -> str:
        digest = hashlib.sha256(
            (self.shop_domain + "\x00" + self.webhook_id).encode("utf-8")
        ).hexdigest()
        return f"events/shopify/v1/{digest[:2]}/{digest}.json"

    def object_bytes(self) -> bytes:
        record = {
            "schema_version": 1,
            "source": "shopify_webhook",
            "shop_domain": self.shop_domain,
            "webhook_id": self.webhook_id,
            "event_id": self.event_id,
            "topic": self.topic,
            "payload": self.payload,
        }
        return json.dumps(
            record, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")


def _aws_error_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", ""))


class SqsToS3LandingConsumer:
    def __init__(self, *, sqs_client, s3_client, queue_url: str, bucket: str):
        if not queue_url.startswith("https://") or not bucket:
            raise ValueError("SQS queue URL and S3 bucket are required")
        self.sqs = sqs_client
        self.s3 = s3_client
        self.queue_url = queue_url
        self.bucket = bucket

    def persist(self, envelope: WebhookEnvelope) -> str:
        """Persist once; return 'created' or 'duplicate'."""
        body = envelope.object_bytes()
        checksum = hashlib.sha256(body).hexdigest()
        key = envelope.object_key()
        try:
            self.s3.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType="application/json",
                Metadata={"sha256": checksum},
                IfNoneMatch="*",
            )
            return "created"
        except ClientError as exc:
            if _aws_error_code(exc) not in {"PreconditionFailed", "412"}:
                raise
        head = self.s3.head_object(Bucket=self.bucket, Key=key)
        if head.get("Metadata", {}).get("sha256") != checksum:
            raise ConflictingReplay("Conflicting payload for existing webhook identity")
        return "duplicate"

    def process_message(self, message: dict) -> str:
        """Delete SQS message only after S3 persistence is confirmed."""
        receipt = message["ReceiptHandle"]
        envelope = WebhookEnvelope.from_sqs_body(message["Body"])
        outcome = self.persist(envelope)
        self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt)
        return outcome

    def poll_once(self, *, wait_seconds: int = 20, max_messages: int = 10) -> dict:
        if not 0 <= wait_seconds <= 20 or not 1 <= max_messages <= 10:
            raise ValueError("Invalid SQS poll options")
        response = self.sqs.receive_message(
            QueueUrl=self.queue_url,
            MaxNumberOfMessages=max_messages,
            WaitTimeSeconds=wait_seconds,
            MessageSystemAttributeNames=["ApproximateReceiveCount"],
        )
        counts = {"received": 0, "created": 0, "duplicate": 0, "failed": 0}
        for message in response.get("Messages", []):
            counts["received"] += 1
            try:
                outcome = self.process_message(message)
            except Exception:
                counts["failed"] += 1
                LOGGER.warning(
                    "SQS landing failed for message id=%s", message.get("MessageId", "?")
                )
            else:
                counts[outcome] += 1
        return counts
