"""Versioned, transactional PostgreSQL migration executor.

Never automatically run a migration on module import or application startup.
The caller supplies an authenticated psycopg connection and a trusted SQL directory.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

MIGRATION_NAME = re.compile(r"^([0-9]{3})_[a-z][a-z0-9_]*\.sql$")
ADVISORY_LOCK_KEY = 735184221  # Project-specific transactional PostgreSQL advisory lock.


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str
    checksum: str


def discover_migrations(directory: Path) -> list[Migration]:
    directory = Path(directory)
    files = sorted(directory.glob("*.sql"))
    if not files:
        raise ValueError(f"No SQL migrations found in {directory}")
    migrations: list[Migration] = []
    for path in files:
        match = MIGRATION_NAME.fullmatch(path.name)
        if not match:
            raise ValueError(f"Unexpected SQL migration filename: {path.name}")
        sql = path.read_text(encoding="utf-8")
        if not sql.strip():
            raise ValueError(f"Empty migration: {path.name}")
        migrations.append(Migration(match.group(1), path.name, sql,
                                    sha256(sql.encode("utf-8")).hexdigest()))
    versions = [m.version for m in migrations]
    if len(set(versions)) != len(versions):
        raise ValueError("Duplicate migration version")
    if versions != [f"{i:03d}" for i in range(1, len(versions) + 1)]:
        raise ValueError("Migration versions must be continuous from 001")
    return migrations


def apply_migrations(connection, migrations: list[Migration]) -> list[str]:
    """Apply pending migrations in a single transaction with an advisory lock.

    Any failure rolls back every migration from this invocation. The database role
    must be authorized for DDL; do not use the normal application role.
    """
    if not migrations:
        raise ValueError("No migration files supplied")
    applied_now: list[str] = []
    with connection.transaction(), connection.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_LOCK_KEY,))
        cur.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version TEXT PRIMARY KEY,
            filename TEXT NOT NULL,
            checksum_sha256 CHAR(64) NOT NULL,
            installed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )""")
        cur.execute("SELECT version, filename, checksum_sha256 FROM schema_migrations ORDER BY version")
        applied = {row[0]: (row[1], row[2]) for row in cur.fetchall()}
        expected = {migration.version: migration for migration in migrations}
        unknown = set(applied) - set(expected)
        if unknown:
            raise RuntimeError(f"Unknown migration versions present in database: {sorted(unknown)}")
        for migration in migrations:
            known = applied.get(migration.version)
            if known is not None:
                if known != (migration.name, migration.checksum):
                    raise RuntimeError(f"Migration checksum mismatch: {migration.version}")
                continue
            # Execute the trusted SQL file as a whole; no naïve semicolon splitting.
            cur.execute(migration.sql)
            cur.execute("""INSERT INTO schema_migrations
                (version, filename, checksum_sha256) VALUES (%s, %s, %s)""",
                (migration.version, migration.name, migration.checksum))
            applied_now.append(migration.version)
    return applied_now
