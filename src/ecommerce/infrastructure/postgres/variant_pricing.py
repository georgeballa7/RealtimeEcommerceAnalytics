"""RDS operational state: write only Shopify-confirmed prices."""
from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from ecommerce.infrastructure.postgres.catalog_identity import _gid


class PostgresVariantPricingRepository:
    def __init__(self, connection, shop_gid: str):
        _gid(shop_gid, "Shop")
        if not connection.autocommit:
            raise ValueError("Requires autocommit connection")
        self.connection = connection
        self.shop_gid = shop_gid

    def verified_variants(self) -> list[tuple[str, str, str, str]]:
        with self.connection.cursor() as cur:
            cur.execute(
                """SELECT sku, variant_id::text, shopify_product_gid, shopify_variant_gid
                   FROM shopify_variant_identity
                   WHERE shop_gid=%s AND status='verified' ORDER BY sku""",
                (self.shop_gid,),
            )
            return [tuple(row) for row in cur.fetchall()]

    def save_confirmed(self, *, variant_id: str, price_gbp: Decimal, manifest_sha256: str):
        UUID(variant_id)
        if not price_gbp.is_finite() or price_gbp <= 0:
            raise ValueError("Invalid verified price")
        if len(manifest_sha256) != 64 or any(c not in "0123456789abcdef" for c in manifest_sha256):
            raise ValueError("Invalid manifest checksum")
        with self.connection.transaction(), self.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO shopify_variant_price_state
                   (shop_gid, variant_id, price_gbp, currency, shopify_verified_at, manifest_sha256)
                   VALUES (%s, %s, %s, 'GBP', now(), %s)
                   ON CONFLICT (shop_gid, variant_id) DO UPDATE SET
                   price_gbp=EXCLUDED.price_gbp, currency=EXCLUDED.currency,
                   shopify_verified_at=EXCLUDED.shopify_verified_at,
                   manifest_sha256=EXCLUDED.manifest_sha256, updated_at=now()""",
                (self.shop_gid, variant_id, price_gbp, manifest_sha256),
            )
