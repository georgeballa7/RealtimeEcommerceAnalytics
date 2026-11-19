"""Apply only Phase 5 operational migration 008 against private RDS.

Earlier Phase 5 pricing migration 007 may have been applied using the separate
pricing procedure. Never replay historical migrations just to add operational
live-order tables. The version/sha is tracked transactionally with an advisory
lock and privileges granted to the existing application role.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import psycopg
from psycopg import sql

VERSION = "008"
NAME = "008_shopify_live_orders.sql"
LOCK = 735184221

def main() -> None:
    root = Path(__file__).resolve().parents[4]
    path = root / "infrastructure" / "sql" / NAME
    if not path.is_file():
        raise RuntimeError("Packaged Phase 5 migration 008 is missing")
    text = path.read_text(encoding="utf-8")
    checksum = hashlib.sha256(text.encode("utf-8")).hexdigest()
    ca = os.environ["DB_SSLROOTCERT"]
    if not Path(ca).is_file():
        raise RuntimeError("Trusted RDS CA bundle missing")
    with psycopg.connect(
        host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"], user=os.environ["DB_ADMIN_USER"],
        password=os.environ["DB_ADMIN_PASSWORD"],
        sslmode="verify-full", sslrootcert=ca, connect_timeout=15,
        autocommit=True,
    ) as conn:
        with conn.transaction(), conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK,))
            cur.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY, filename TEXT NOT NULL,
                checksum_sha256 CHAR(64) NOT NULL,
                installed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
            cur.execute("SELECT filename, checksum_sha256 FROM schema_migrations WHERE version=%s", (VERSION,))
            previous = cur.fetchone()
            if previous:
                if tuple(previous) != (NAME, checksum):
                    raise RuntimeError("Migration 008 checksum or filename changed")
                status = "already_applied"
            else:
                cur.execute(text)
                cur.execute("""INSERT INTO schema_migrations(version, filename, checksum_sha256)
                               VALUES (%s, %s, %s)""", (VERSION, NAME, checksum))
                status = "applied"
            role = os.environ.get("DB_APP_USER", "ecommerce_app")
            for table in ("shopify_live_order", "shopify_live_order_line",
                          "shopify_live_refund", "shopify_live_refund_line",
                          "shopify_live_event_ledger", "shopify_simulation_run"):
                cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {} TO {}").format(
                    sql.Identifier(table), sql.Identifier(role)))
            cur.execute(sql.SQL("GRANT SELECT ON shopify_live_refund_reconciliation TO {}").format(
                sql.Identifier(role)))
    print("Phase 5 operational schema:", status, "version:", VERSION)

if __name__ == "__main__":
    main()
