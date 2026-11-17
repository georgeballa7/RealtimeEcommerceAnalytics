"""PostgreSQL customer mappings and restart-safe sync state.

An application PostgreSQL role, not the RDS master account, must execute this.
"""
from __future__ import annotations
from dataclasses import dataclass


@dataclass(frozen=True)
class HistoricalCustomer:
    customer_id: str
    source_customer_id: str


class PostgresCustomerIdentityRepository:
    def __init__(self, connection, shop_gid: str):
        if not shop_gid.startswith("gid://shopify/Shop/"):
            raise ValueError("Invalid shop GID")
        self.connection = connection
        self.shop_gid = shop_gid

    def get(self, customer_id: str) -> str | None:
        with self.connection.cursor() as cur:
            cur.execute("""SELECT shopify_customer_gid FROM shopify_customer_identity
                           WHERE shop_gid = %s AND customer_id = %s""", (self.shop_gid, customer_id))
            row = cur.fetchone()
            return row[0] if row else None

    def save_verified(self, customer: HistoricalCustomer, gid: str) -> None:
        if not gid.startswith("gid://shopify/Customer/"):
            raise ValueError("Invalid customer GID")
        # DB uniqueness + conditional upsert detects source/customer remapping.
        with self.connection.transaction():
            with self.connection.cursor() as cur:
                cur.execute("""INSERT INTO shopify_customer_identity
                    (shop_gid, customer_id, source_system, source_customer_id, shopify_customer_gid, status)
                    VALUES (%s, %s, 'uci_online_retail_ii', %s, %s, 'verified')
                    ON CONFLICT (shop_gid, customer_id) DO UPDATE SET
                        shopify_customer_gid = EXCLUDED.shopify_customer_gid,
                        status = 'verified', updated_at = now()
                    WHERE shopify_customer_identity.shopify_customer_gid = EXCLUDED.shopify_customer_gid
                      AND shopify_customer_identity.source_customer_id = EXCLUDED.source_customer_id
                    RETURNING customer_id""",
                    (self.shop_gid, customer.customer_id, customer.source_customer_id, gid))
                if cur.fetchone() is None:
                    raise ValueError("Conflicting Shopify customer mapping")

    def count_verified(self) -> int:
        with self.connection.cursor() as cur:
            cur.execute("""SELECT count(*) FROM shopify_customer_identity
                           WHERE shop_gid = %s AND status = 'verified'""", (self.shop_gid,))
            return int(cur.fetchone()[0])
