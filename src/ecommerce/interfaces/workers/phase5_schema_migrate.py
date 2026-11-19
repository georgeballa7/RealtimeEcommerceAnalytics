"""One-off versioned Phase 5 schema migration using the RDS admin secret.

Executed inside a private ECS task after Terraform apply, before webhook
registration or any synthetic Shopify orders. No migration at app startup.
"""
from __future__ import annotations
import os
from pathlib import Path
import psycopg
from psycopg import sql
from ecommerce.infrastructure.postgres.migrations import apply_migrations, discover_migrations

def main() -> None:
    root = Path(__file__).resolve().parents[4]
    sql_dir = root / "infrastructure" / "sql"
    if not sql_dir.is_dir():
        raise RuntimeError("Packaged SQL migration directory is missing")
    ca = os.environ["DB_SSLROOTCERT"]
    if not Path(ca).is_file():
        raise RuntimeError("Trusted RDS CA bundle missing")
    migrations = discover_migrations(sql_dir)
    if not any(m.version == "008" for m in migrations):
        raise RuntimeError("Expected operational migration 008 is absent")
    with psycopg.connect(
        host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", "5432")),
        dbname=os.environ["DB_NAME"], user=os.environ["DB_ADMIN_USER"],
        password=os.environ["DB_ADMIN_PASSWORD"],
        sslmode="verify-full", sslrootcert=ca, connect_timeout=15
    ) as conn:
        applied = apply_migrations(conn, migrations)
        role = os.environ.get("DB_APP_USER", "ecommerce_app")
        with conn.transaction(), conn.cursor() as cur:
            # psycopg Identifier quotes a role safely.
            for table in ("shopify_live_order", "shopify_live_order_line",
                          "shopify_live_refund", "shopify_live_refund_line",
                          "shopify_live_event_ledger", "shopify_simulation_run"):
                cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {} TO {}").format(
                    sql.Identifier(table), sql.Identifier(role)))
            cur.execute(sql.SQL("GRANT SELECT ON shopify_live_refund_reconciliation TO {}").format(
                sql.Identifier(role)))
    print("Phase 5 schema verified, migration versions newly applied:", applied)

if __name__ == "__main__":
    main()
