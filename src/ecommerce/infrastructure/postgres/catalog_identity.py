"""Shop-scoped verified catalog identities; no guessed Shopify GIDs."""
from __future__ import annotations

from uuid import UUID

from ecommerce.application.use_cases.catalog_selection import CatalogCandidate


def _gid(value: str, entity: str) -> str:
    prefix = f"gid://shopify/{entity}/"
    if not isinstance(value, str) or not value.startswith(prefix):
        raise ValueError(f"Invalid Shopify {entity} GID")
    tail = value.removeprefix(prefix)
    if not tail.isdecimal() or not tail:
        raise ValueError(f"Invalid Shopify {entity} GID")
    return value


class PostgresCatalogIdentityRepository:
    """Requires an autocommit connection; each mapping is a durable transaction."""

    def __init__(self, connection, shop_gid: str):
        _gid(shop_gid, "Shop")
        if not connection.autocommit:
            raise ValueError("Catalog mapping connection requires autocommit=True")
        self.connection = connection
        self.shop_gid = shop_gid

    def get_product(self, product_id: str) -> str | None:
        UUID(product_id)
        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT shopify_product_gid FROM shopify_product_identity "
                "WHERE shop_gid=%s AND product_id=%s AND status='verified'",
                (self.shop_gid, product_id),
            )
            row = cur.fetchone()
            return row[0] if row else None

    def get_variant(self, variant_id: str) -> tuple[str, str] | None:
        UUID(variant_id)
        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT shopify_product_gid, shopify_variant_gid "
                "FROM shopify_variant_identity "
                "WHERE shop_gid=%s AND variant_id=%s AND status='verified'",
                (self.shop_gid, variant_id),
            )
            row = cur.fetchone()
            return (row[0], row[1]) if row else None

    def save_product(self, candidate: CatalogCandidate, product_gid: str) -> None:
        _gid(product_gid, "Product")
        with self.connection.transaction(), self.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO shopify_product_identity
                   (shop_gid, product_id, source_system, source_stock_code,
                    shopify_product_gid, status)
                   VALUES (%s, %s, 'uci_online_retail_ii', %s, %s, 'verified')
                   ON CONFLICT (shop_gid, product_id) DO UPDATE SET
                     verified_at = now()
                   WHERE shopify_product_identity.source_system = EXCLUDED.source_system
                     AND shopify_product_identity.source_stock_code = EXCLUDED.source_stock_code
                     AND shopify_product_identity.shopify_product_gid = EXCLUDED.shopify_product_gid
                   RETURNING product_id""",
                (self.shop_gid, candidate.product_id, candidate.source_stock_code, product_gid),
            )
            if cur.fetchone() is None:
                raise ValueError("Conflicting product identity mapping")

    def save_variant(
        self, candidate: CatalogCandidate, product_gid: str, variant_gid: str
    ) -> None:
        _gid(product_gid, "Product")
        _gid(variant_gid, "ProductVariant")
        with self.connection.transaction(), self.connection.cursor() as cur:
            cur.execute(
                """INSERT INTO shopify_variant_identity
                   (shop_gid, variant_id, product_id, shopify_product_gid,
                    sku, shopify_variant_gid, status)
                   VALUES (%s, %s, %s, %s, %s, %s, 'verified')
                   ON CONFLICT (shop_gid, variant_id) DO UPDATE SET
                     verified_at = now()
                   WHERE shopify_variant_identity.product_id = EXCLUDED.product_id
                     AND shopify_variant_identity.shopify_product_gid = EXCLUDED.shopify_product_gid
                     AND shopify_variant_identity.sku = EXCLUDED.sku
                     AND shopify_variant_identity.shopify_variant_gid = EXCLUDED.shopify_variant_gid
                   RETURNING variant_id""",
                (
                    self.shop_gid, candidate.variant_id, candidate.product_id,
                    product_gid, candidate.sku, variant_gid,
                ),
            )
            if cur.fetchone() is None:
                raise ValueError("Conflicting variant identity mapping")

    def count_verified(self) -> tuple[int, int]:
        with self.connection.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM shopify_product_identity "
                "WHERE shop_gid=%s AND status='verified'",
                (self.shop_gid,),
            )
            products = int(cur.fetchone()[0])
            cur.execute(
                "SELECT count(*) FROM shopify_variant_identity "
                "WHERE shop_gid=%s AND status='verified'",
                (self.shop_gid,),
            )
            variants = int(cur.fetchone()[0])
            return products, variants
