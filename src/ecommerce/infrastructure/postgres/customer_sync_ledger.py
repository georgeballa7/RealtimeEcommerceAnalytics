"""Durable sync audit and time-bounded customer lease in PostgreSQL.

Connection MUST use autocommit=True so each record and lease claim is committed
before an external Shopify call. Only sanitized failure codes are stored.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from uuid import UUID, uuid4


class CustomerLeaseBusy(RuntimeError):
    """Another worker already owns an unexpired lease for this customer."""


@dataclass(frozen=True)
class AttemptEvent:
    phase: str
    outcome: str
    shopify_gid: str | None = None
    failure_code: str | None = None


class PostgresCustomerSyncLedger:
    def __init__(self, connection, shop_gid: str, lease_seconds: int = 900):
        if not shop_gid.startswith("gid://shopify/Shop/"):
            raise ValueError("Invalid shop GID")
        if not 60 <= lease_seconds <= 3600:
            raise ValueError("Lease duration must be between 60 and 3600 seconds")
        if not connection.autocommit:
            raise ValueError("Ledger requires autocommit=True for durable boundaries")
        self.connection = connection
        self.shop_gid = shop_gid
        self.lease_seconds = lease_seconds
        self._active_token = ContextVar("customer_sync_lease_token", default=None)

    @contextmanager
    def claim(self, customer_id: str):
        """A fresh UUID token prevents releasing another worker's reclaimed lease."""
        UUID(customer_id)
        token = str(uuid4())
        with self.connection.transaction():
            with self.connection.cursor() as cur:
                cur.execute("""INSERT INTO shopify_customer_sync_lease
                    (shop_gid, customer_id, lease_token, lease_expires_at)
                    VALUES (%s, %s, %s, now() + (%s * interval '1 second'))
                    ON CONFLICT (shop_gid, customer_id) DO UPDATE SET
                        lease_token = EXCLUDED.lease_token,
                        lease_expires_at = EXCLUDED.lease_expires_at,
                        updated_at = now()
                    WHERE shopify_customer_sync_lease.lease_expires_at <= now()
                    RETURNING lease_token""",
                    (self.shop_gid, customer_id, token, self.lease_seconds))
                if cur.fetchone() is None:
                    raise CustomerLeaseBusy("Customer sync lease already owned")
        context_token = self._active_token.set((customer_id, token))
        try:
            yield
        finally:
            self._active_token.reset(context_token)
            with self.connection.transaction():
                with self.connection.cursor() as cur:
                    cur.execute("""DELETE FROM shopify_customer_sync_lease
                        WHERE shop_gid = %s AND customer_id = %s AND lease_token = %s""",
                        (self.shop_gid, customer_id, token))

    def record(self, customer_id: str, event: AttemptEvent) -> None:
        """Append one event; require a currently owned lease to serialize sequence."""
        UUID(customer_id)
        active = self._active_token.get()
        if active is None or active[0] != customer_id:
            raise CustomerLeaseBusy("No active lease owned by this worker")
        if event.phase not in {"lookup", "upsert", "reconcile", "persist"}:
            raise ValueError("Invalid sync event phase")
        if event.outcome not in {"started", "verified", "not_found", "retryable", "ambiguous", "failed"}:
            raise ValueError("Invalid sync event outcome")
        if event.failure_code is not None and not (event.failure_code.isascii() and
                1 <= len(event.failure_code) <= 48 and
                all(c.isupper() or c.isdigit() or c == '_' for c in event.failure_code)):
            raise ValueError("Expected a sanitized uppercase failure code")
        with self.connection.transaction():
            with self.connection.cursor() as cur:
                cur.execute("""SELECT 1 FROM shopify_customer_sync_lease
                    WHERE shop_gid=%s AND customer_id=%s AND lease_token=%s
                      AND lease_expires_at > now() FOR UPDATE""",
                    (self.shop_gid, customer_id, active[1]))
                if cur.fetchone() is None:
                    raise CustomerLeaseBusy("Customer lease expired or absent")
                cur.execute("""INSERT INTO shopify_customer_sync_attempt
                    (shop_gid, customer_id, attempt_number, phase, outcome,
                     shopify_customer_gid, failure_code)
                    SELECT %s, %s, COALESCE(MAX(attempt_number), 0) + 1,
                           %s, %s, %s, %s
                    FROM shopify_customer_sync_attempt
                    WHERE shop_gid=%s AND customer_id=%s""",
                    (self.shop_gid, customer_id, event.phase, event.outcome,
                     event.shopify_gid, event.failure_code, self.shop_gid, customer_id))
