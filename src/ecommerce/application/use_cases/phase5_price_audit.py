"""Read-only verification of approved GBP pricing against PostgreSQL state."""
from __future__ import annotations

from decimal import Decimal


def audit_price_state(connection, manifest: dict, manifest_sha256: str) -> dict:
    """Compare each approved (SKU, variant UUID, GBP price) with RDS rows.

    Caller must enforce a PostgreSQL read-only session; this function uses
    only SELECT queries and never opens a Shopify API connection.
    """
    approved = {item["sku"]: (item["canonical_variant_id"], Decimal(item["price_gbp"]))
                for item in manifest["prices"]}
    if len(approved) != 3408 or manifest["count"] != 3408:
        raise ValueError("Unexpected manifest population")
    with connection.cursor() as cur:
        cur.execute(
            """SELECT i.sku, i.variant_id::text, p.price_gbp::text, p.currency,
                      trim(p.manifest_sha256), p.shopify_verified_at
               FROM shopify_variant_identity AS i
               LEFT JOIN shopify_variant_price_state AS p
                 ON (p.shop_gid=i.shop_gid AND p.variant_id=i.variant_id)
               WHERE i.status = 'verified'"""
        )
        rows = cur.fetchall()
        cur.execute("SELECT count(*) FROM shopify_variant_price_state")
        price_rows = int(cur.fetchone()[0])
    observed = {}
    errors = []
    for sku, variant_uuid, price, currency, checksum, verified_at in rows:
        if sku in observed:
            errors.append("Duplicate verified SKU: " + sku)
        observed[sku] = (variant_uuid, price, currency, checksum, verified_at)
    good = 0
    for sku, (uuid, target_price) in approved.items():
        item = observed.get(sku)
        if item is None:
            errors.append("Missing verified SKU: " + sku)
        elif item[0] != uuid:
            errors.append("Variant identity mismatch: " + sku)
        elif item[1] is None:
            errors.append("Missing RDS price: " + sku)
        elif Decimal(item[1]) != target_price:
            errors.append("GBP price mismatch: " + sku)
        elif item[2] != "GBP":
            errors.append("Currency mismatch: " + sku)
        elif item[3] != manifest_sha256:
            errors.append("Manifest checksum mismatch: " + sku)
        elif item[4] is None:
            errors.append("Missing verification timestamp: " + sku)
        else:
            good += 1
    for sku in observed:
        if sku not in approved:
            errors.append("Unexpected verified SKU: " + sku)
    if price_rows != 3408:
        errors.append(f"Unexpected price-state row count: {price_rows}")
    report = {
        "mode": "READ_ONLY_RDS_AUDIT",
        "expected": len(approved),
        "verified_identity_rows": len(rows),
        "rds_price_rows": price_rows,
        "matched": good,
        "errors": len(errors),
        "sample_errors": errors[:10],
        "manifest_sha256": manifest_sha256,
    }
    return report
