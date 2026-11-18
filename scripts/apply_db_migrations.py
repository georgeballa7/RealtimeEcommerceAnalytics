"""Run versioned migrations against private RDS from an approved AWS task.

Default action is a *local plan*; it makes no database connection or writes.
Use --apply only within the reviewed AWS migration task with a DDL-capable role.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from ecommerce.infrastructure.postgres.migrations import apply_migrations, discover_migrations

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Connect and apply migrations")
    parser.add_argument("--sql-dir", type=Path,
                        default=ROOT / "infrastructure" / "sql")
    args = parser.parse_args()
    migrations = discover_migrations(args.sql_dir)
    print("Migrations:", ", ".join(m.name for m in migrations))
    if not args.apply:
        print("PLAN ONLY: no database connection or SQL execution")
        return
    dsn = os.environ.get("ECOMMERCE_MIGRATION_DATABASE_URL")
    if not dsn:
        parser.error("ECOMMERCE_MIGRATION_DATABASE_URL is required for --apply")
    import psycopg
    # Explicit TLS peer verification and a trusted RDS CA root bundle are required.
    ca = os.environ.get("ECOMMERCE_RDS_CA_BUNDLE")
    if not ca or not Path(ca).is_file():
        parser.error("ECOMMERCE_RDS_CA_BUNDLE must point to a readable trusted CA bundle")
    with psycopg.connect(dsn, sslmode="verify-full", sslrootcert=ca, autocommit=True) as conn:
        applied = apply_migrations(conn, migrations)
    print("Newly applied migration versions:", applied)


if __name__ == "__main__":
    main()
