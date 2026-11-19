"""Replay-safe, conditional S3 landing for verified Shopify webhook envelopes."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from botocore.exceptions import ClientError


class InvalidEvent(ValueError):
    pass


class ConflictingReplay(RuntimeError):
    pass


@dataclass(frozen=True)
class WebhookEnvelope:
    shop_domain: str
    webhook_id: str
    topic: str
    event_id: str | None
    payload: dict

    @classmethod
    def from_sqs_body(cls, text):
        if not isinstance(text, str) or len(text.encode('utf-8')) > 1_048_576:
            raise InvalidEvent('Invalid message size')
        try:
            data = json.loads(text)
        except (ValueError, TypeError) as exc:
            raise InvalidEvent('Invalid JSON') from exc
        if not isinstance(data, dict) or data.get('schema_version') != 1 or data.get('source') != 'shopify_webhook':
            raise InvalidEvent('Invalid envelope')
        domain, webhook_id, topic, payload = (data.get(field) for field in ('shop_domain','webhook_id','topic','payload'))
        if (not isinstance(domain, str) or not domain.endswith('.myshopify.com')
            or not domain.isascii() or any(x not in 'abcdefghijklmnopqrstuvwxyz0123456789-.' for x in domain)
            or len(domain) > 253 or domain.startswith('.') or '..' in domain):
            raise InvalidEvent('Invalid shop')
        if (not isinstance(webhook_id, str) or not 0 < len(webhook_id) <= 200
            or not isinstance(topic, str) or not 0 < len(topic) <= 200 or not isinstance(payload, dict)):
            raise InvalidEvent('Invalid webhook content')
        event_id = data.get('event_id')
        if event_id is not None and not isinstance(event_id, str):
            raise InvalidEvent('Invalid event id')
        return cls(domain, webhook_id, topic, event_id, payload)

    def object_key(self):
        digest = hashlib.sha256((self.shop_domain + '\x00' + self.webhook_id).encode()).hexdigest()
        return f'events/shopify/v1/{digest[:2]}/{digest}.json'

    def object_bytes(self):
        return json.dumps({'schema_version': 1, 'source': 'shopify_webhook',
                           'shop_domain': self.shop_domain, 'webhook_id': self.webhook_id,
                           'topic': self.topic, 'event_id': self.event_id, 'payload': self.payload},
                          sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')


class SqsToS3LandingConsumer:
    def __init__(self, *, sqs_client, s3_client, queue_url, bucket):
        if not bucket or not queue_url.startswith('https://'):
            raise ValueError('Missing configured queue/bucket')
        self.s3, self.bucket = s3_client, bucket
        self.sqs, self.queue_url = sqs_client, queue_url

    def persist(self, envelope):
        data = envelope.object_bytes()
        key = envelope.object_key()
        digest = hashlib.sha256(data).hexdigest()
        try:
            self.s3.put_object(Bucket=self.bucket, Key=key, Body=data,
                               ContentType='application/json', Metadata={'sha256':digest},
                               IfNoneMatch='*')
            return 'created'
        except ClientError as exc:
            if exc.response.get('Error',{}).get('Code') not in ('PreconditionFailed','412'):
                raise
        head = self.s3.head_object(Bucket=self.bucket, Key=key)
        if head.get('Metadata',{}).get('sha256') != digest:
            raise ConflictingReplay('Webhook duplicate has conflicting data')
        return 'duplicate'

    def process_message(self, message: dict) -> str:
        """Legacy ECS polling path: acknowledge SQS only after durable S3 landing.

        New Lambda event-source processing calls persist() directly and lets
        Lambda manage SQS acknowledgements via ReportBatchItemFailures.
        """
        if self.sqs is None:
            raise RuntimeError("SQS polling client is not configured")
        if not isinstance(message, dict):
            raise InvalidEvent("Invalid SQS message")
        body, receipt = message.get("Body"), message.get("ReceiptHandle")
        if not isinstance(receipt, str) or not receipt:
            raise InvalidEvent("Missing SQS receipt handle")
        envelope = WebhookEnvelope.from_sqs_body(body)
        outcome = self.persist(envelope)
        # Any failed delete propagates: SQS redelivers and S3 deduplicates.
        self.sqs.delete_message(QueueUrl=self.queue_url, ReceiptHandle=receipt)
        return outcome

    def poll_once(self, *, wait_seconds: int = 20, max_messages: int = 10) -> dict[str, int]:
        """Backwards-compatible ECS long-polling loop; no silent acknowledgements."""
        if (isinstance(wait_seconds, bool) or not isinstance(wait_seconds, int)
                or not 0 <= wait_seconds <= 20):
            raise ValueError("wait_seconds must be between 0 and 20")
        if (isinstance(max_messages, bool) or not isinstance(max_messages, int)
                or not 1 <= max_messages <= 10):
            raise ValueError("max_messages must be between 1 and 10")
        if self.sqs is None:
            raise RuntimeError("SQS polling client is not configured")
        response = self.sqs.receive_message(
            QueueUrl=self.queue_url,
            WaitTimeSeconds=wait_seconds,
            MaxNumberOfMessages=max_messages,
        )
        messages = response.get("Messages", [])
        counts = {"received": len(messages), "created": 0, "duplicate": 0, "failed": 0}
        for message in messages:
            try:
                outcome = self.process_message(message)
            except Exception:
                # Leave failed/invalid messages in SQS for retry then DLQ.
                counts["failed"] += 1
            else:
                counts[outcome] += 1
        return counts
