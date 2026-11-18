"""Durable shop-scoped catalog product lease for concurrent ECS reconciliation."""
from __future__ import annotations

from contextlib import contextmanager
from uuid import UUID, uuid4


class CatalogLeaseBusy(RuntimeError):
    """Another worker already owns the product lease."""


class PostgresCatalogLease:
    def __init__(self, connection, shop_gid: str, lease_seconds: int = 900):
        if not connection.autocommit:
            raise ValueError("Autocommit required for durable external boundaries")
        if not shop_gid.startswith("gid://shopify/Shop/"):
            raise ValueError("Invalid Shopify shop")
        if not 60 <= lease_seconds <= 3600:
            raise ValueError("Lease length out of bounds")
        self.connection = connection
        self.shop_gid = shop_gid
        self.lease_seconds = lease_seconds

    @contextmanager
    def claim(self, product_id: str):
        UUID(product_id)
        token = str(uuid4())
        with self.connection.transaction(), self.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO shopify_catalog_sync_lease
                   (shop_gid, product_id, lease_token, lease_expires_at)
                   VALUES (%s, %s, %s, now() + (%s * INTERVAL '1 second'))
                   ON CONFLICT (shop_gid, product_id) DO UPDATE SET
                     lease_token = EXCLUDED.lease_token,
                     lease_expires_at = EXCLUDED.lease_expires_at,
                     updated_at = now()
                   WHERE shopify_catalog_sync_lease.lease_expires_at <= now()
                   RETURNING lease_token""",
                (self.shop_gid, product_id, token, self.lease_seconds),
            )
            if cur.fetchone() is None:
                raise CatalogLeaseBusy("Product sync lease is already owned")
        try:
            yield
        finally:
            # Token fence prevents a slow, expired owner releasing the new owner.
            with self.connection.transaction(), self.connection.cursor() as cur:
                cur.execute(
                    """DELETE FROM shopify_catalog_sync_lease
                       WHERE shop_gid=%s AND product_id=%s AND lease_token=%s""",
                    (self.shop_gid, product_id, token),
                )
