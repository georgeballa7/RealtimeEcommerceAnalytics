"""Atomic PostgreSQL webhook inbox/outbox persistence.

Do not send SQS messages from the webhook transaction: the durable outbox
publisher is a separate component with retries and reconciliation.
"""
from __future__ import annotations

from dataclasses import dataclass

from psycopg.types.json import Jsonb


@dataclass(frozen=True)
class IncomingWebhook:
    shop_domain: str
    webhook_id: str
    event_id: str | None
    topic: str
    payload: dict


class PostgresWebhookInbox:
    def __init__(self, connection_factory):
        self.connection_factory = connection_factory

    def accept(self, event: IncomingWebhook) -> bool:
        """Return False on duplicate. Both inbox and outbox commit together."""
        with self.connection_factory() as connection, connection.transaction(), connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO shopify_webhook_inbox
                    (shop_domain, webhook_id, event_id, topic, payload)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (shop_domain, webhook_id) DO NOTHING
                RETURNING id
                """,
                (
                    event.shop_domain,
                    event.webhook_id,
                    event.event_id,
                    event.topic,
                    Jsonb(event.payload),
                ),
            )
            row = cursor.fetchone()
            if row is None:
                return False
            cursor.execute(
                "INSERT INTO shopify_webhook_outbox (inbox_id) VALUES (%s)",
                (row[0],),
            )
        return True
