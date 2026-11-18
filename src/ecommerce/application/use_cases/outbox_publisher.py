"""Lease-based transactional outbox publisher for PostgreSQL -> SQS.

An SQS send and a PostgreSQL commit cannot be atomic: this publisher provides
at-least-once delivery. The S3 landing consumer MUST deduplicate by webhook key.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from uuid import uuid4

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ClaimedEvent:
    outbox_id: int
    webhook_id: str
    shop_domain: str
    topic: str
    event_id: str | None
    payload: dict
    claim_token: str


class PostgresOutbox:
    def __init__(self, connection_factory):
        self.connection_factory = connection_factory

    def claim(self, lease_seconds: int = 120) -> ClaimedEvent | None:
        """Atomically claim one available row using SKIP LOCKED.

        A crashed sender's lease expires and becomes claimable again.
        """
        if lease_seconds < 15:
            raise ValueError("Lease must be at least 15 seconds")
        token = str(uuid4())
        with self.connection_factory() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        WITH candidate AS (
                          SELECT id FROM shopify_webhook_outbox
                          WHERE (status = 'pending' AND next_attempt_at <= now())
                             OR (status = 'publishing' AND lease_until < now())
                          ORDER BY id
                          FOR UPDATE SKIP LOCKED LIMIT 1
                        )
                        UPDATE shopify_webhook_outbox AS o
                        SET status = 'publishing', claim_token = %s,
                            lease_until = now() + (%s * INTERVAL '1 second'),
                            attempts = attempts + 1
                        FROM candidate
                        WHERE o.id = candidate.id
                        RETURNING o.id, o.inbox_id
                        """,
                        (token, lease_seconds),
                    )
                    claimed = cursor.fetchone()
                    if claimed is None:
                        return None
                    cursor.execute(
                        """
                        SELECT webhook_id, shop_domain, topic, event_id, payload
                        FROM shopify_webhook_inbox WHERE id = %s
                        """,
                        (claimed[1],),
                    )
                    webhook = cursor.fetchone()
                    if webhook is None:
                        raise RuntimeError("Outbox record has no inbox row")
        return ClaimedEvent(
            outbox_id=claimed[0], webhook_id=webhook[0],
            shop_domain=webhook[1], topic=webhook[2],
            event_id=webhook[3], payload=webhook[4], claim_token=token,
        )

    def mark_published(self, event: ClaimedEvent) -> bool:
        with self.connection_factory() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE shopify_webhook_outbox
                        SET status = 'published', published_at = now(),
                            lease_until = NULL, claim_token = NULL, last_error = NULL
                        WHERE id = %s AND status = 'publishing'
                          AND claim_token = %s AND lease_until > now()
                        RETURNING id
                        """,
                        (event.outbox_id, event.claim_token),
                    )
                    return cursor.fetchone() is not None

    def schedule_retry(self, event: ClaimedEvent, *, delay_seconds: int = 30) -> bool:
        if delay_seconds < 1:
            raise ValueError("Retry delay must be positive")
        with self.connection_factory() as connection:
            with connection.transaction():
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE shopify_webhook_outbox
                        SET status = 'pending', claim_token = NULL,
                            lease_until = NULL,
                            next_attempt_at = now() + (%s * INTERVAL '1 second'),
                            last_error = 'SQS delivery failed; retry scheduled'
                        WHERE id = %s AND status = 'publishing'
                          AND claim_token = %s
                        RETURNING id
                        """,
                        (delay_seconds, event.outbox_id, event.claim_token),
                    )
                    return cursor.fetchone() is not None


class SqsOutboxPublisher:
    def __init__(self, repository: PostgresOutbox, sqs_client, queue_url: str):
        if not queue_url.startswith('https://'):
            raise ValueError('A valid SQS queue URL is required')
        self.repository = repository
        self.sqs_client = sqs_client
        self.queue_url = queue_url

    def publish_one(self) -> str:
        """Return 'idle', 'published', 'failed' or 'lease_lost'.

        Message body includes stable identity; never log customer payloads.
        """
        event = self.repository.claim()
        if event is None:
            return 'idle'
        message = {
            'schema_version': 1,
            'source': 'shopify_webhook',
            'shop_domain': event.shop_domain,
            'webhook_id': event.webhook_id,
            'event_id': event.event_id,
            'topic': event.topic,
            'payload': event.payload,
        }
        try:
            self.sqs_client.send_message(
                QueueUrl=self.queue_url,
                MessageBody=json.dumps(message, separators=(',', ':'), ensure_ascii=False),
                MessageAttributes={
                    'topic': {'DataType': 'String', 'StringValue': event.topic},
                },
            )
        except Exception:
            # Return the event to pending; callers must never treat it as sent.
            # Do not log exception details: SDK exceptions may contain sensitive data.
            LOGGER.warning('Outbox SQS publish failed for id=%s', event.outbox_id)
            self.repository.schedule_retry(event)
            return 'failed'
        if not self.repository.mark_published(event):
            # A lost lease can cause another sender to publish the same event.
            # The consumer must dedupe on (shop_domain, webhook_id).
            LOGGER.warning('Outbox lease lost after send for id=%s', event.outbox_id)
            return 'lease_lost'
        return 'published'
