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
