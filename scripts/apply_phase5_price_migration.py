"""Apply ONLY Phase 5 migration 007 using the existing RDS master secret.

Runs within the private ECS task; does not rotate or retrieve application passwords.
Migrations 001-006 were bootstrapped separately and are intentionally NOT replayed.
The default action is a read-only preflight; --apply is required for DDL.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo

SQL_PATH = Path(__file__).resolve().parent / "sql" / "007_shopify_variant_price_state.sql"
LOCK_KEY = 735184221
ALLOWED_APP_ROLE = "ecommerce_app"


def _validate_environment() -> dict[str, str]:
    keys = ("DB_HOST", "DB_NAME", "DB_ADMIN_USER", "DB_ADMIN_PASSWORD",
            "DB_SSLROOTCERT", "DB_APP_USER")
    missing = [name for name in keys if not os.environ.get(name)]
    if missing:
        raise ValueError("Missing database environment settings: " + ", ".join(missing))
    if os.environ["DB_APP_USER"] != ALLOWED_APP_ROLE:
        raise ValueError("Unexpected application database role")
    if not os.path.isfile(os.environ["DB_SSLROOTCERT"]):
        raise ValueError("RDS certificate bundle unavailable")
    if not SQL_PATH.is_file():
        raise ValueError("Migration 007 SQL file unavailable")
    return {name: os.environ[name] for name in keys}


def run(*, connection, apply: bool, migration_sql: str, app_role: str) -> str:
    """Lock and validate existing schema; apply one transaction when authorized."""
    if app_role != ALLOWED_APP_ROLE:
        raise ValueError("Unexpected application role")
    checksum = hashlib.sha256(migration_sql.encode("utf-8")).hexdigest()
    if not migration_sql.startswith("-- Verified operational GBP state"):
        raise ValueError("Unrecognized Phase 5 migration source")
    with connection.transaction(), connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
        cur.execute("SELECT to_regclass('public.shopify_product_identity'), "
                    "to_regclass('public.shopify_variant_identity'), "
                    "to_regclass('public.shopify_variant_price_state'), "
                    "to_regclass('public.schema_migrations')")
        product_table, variant_table, price_table, history_table = cur.fetchone()
        if product_table is None or variant_table is None:
            raise RuntimeError("Catalog identity migration 006 is not installed")
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (app_role,))
        if cur.fetchone() is None:
            raise RuntimeError("Existing application role is missing")
        if history_table is not None:
            cur.execute("SELECT filename, checksum_sha256 FROM schema_migrations "
                        "WHERE version='007'")
            recorded = cur.fetchone()
        else:
            recorded = None
        expected = ("007_shopify_variant_price_state.sql", checksum)
        if recorded is not None:
            if tuple(recorded) != expected or price_table is None:
                raise RuntimeError("Existing migration 007 record/schema mismatch")
            state = "already_applied"
        elif price_table is not None:
            raise RuntimeError("Price table exists without migration 007 record; manual review required")
        else:
            state = "pending"
        if not apply:
            return state
        if state == "pending":
            cur.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                filename TEXT NOT NULL,
                checksum_sha256 CHAR(64) NOT NULL,
                installed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )""")
            cur.execute(migration_sql, prepare=False)
            cur.execute("INSERT INTO schema_migrations(version, filename, checksum_sha256) "
                        "VALUES ('007', %s, %s)", expected)
        cur.execute(
            sql.SQL("GRANT SELECT, INSERT, UPDATE ON TABLE public.shopify_variant_price_state "
                    "TO {}").format(sql.Identifier(app_role))
        )
        cur.execute("SELECT has_table_privilege(%s, "
                    "'public.shopify_variant_price_state', 'SELECT,INSERT,UPDATE')",
                    (app_role,))
        if cur.fetchone()[0] is not True:
            raise RuntimeError("Application price-state grants were not verified")
        cur.execute("SELECT to_regclass('public.shopify_variant_price_state')")
        if cur.fetchone()[0] is None:
            raise RuntimeError("Price-state table creation failed")
        return "applied" if state == "pending" else "already_applied"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Authorize migration 007 DDL and grants")
    args = parser.parse_args()
    config = _validate_environment()
    raw = SQL_PATH.read_text(encoding="utf-8")
    dsn = make_conninfo("", host=config["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
                        dbname=config["DB_NAME"], user=config["DB_ADMIN_USER"],
                        password=config["DB_ADMIN_PASSWORD"], connect_timeout=10)
    with psycopg.connect(dsn, sslmode="verify-full",
                         sslrootcert=config["DB_SSLROOTCERT"], autocommit=True) as conn:
        outcome = run(connection=conn, apply=args.apply,
                      migration_sql=raw, app_role=config["DB_APP_USER"])
    print(f"Phase 5 migration 007: {outcome}; applied={args.apply}", flush=True)


if __name__ == "__main__":
    main()
