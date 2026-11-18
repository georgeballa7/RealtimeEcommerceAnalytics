"""One-off RDS bootstrap task. Run ONLY inside the private application VPC.

Password bytes never appear in Terraform configuration, state, CLI arguments or logs.
The RDS-managed master credential is supplied through an ECS secret injection.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import boto3
import psycopg
from psycopg import sql
from psycopg.conninfo import make_conninfo


MIGRATIONS = (
    ("shopify_customer_identity", "001_shopify_customer_identity.sql"),
    ("shopify_customer_sync_attempt", "002_shopify_customer_sync_ledger.sql"),
    ("shopify_customer_sync_lease", "003_shopify_customer_sync_lease.sql"),
)
APP_TABLES = tuple(table for table, _ in MIGRATIONS)


def apply_migrations(conn) -> None:
    """Create the three required tables once, before provisioning the app role."""
    migration_dir = Path(__file__).resolve().parent / "sql"
    for table, filename in MIGRATIONS:
        row = conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()
        if row and row[0] is not None:
            continue
        statement = (migration_dir / filename).read_text(encoding="utf-8")
        conn.execute(statement, prepare=False)


def bootstrap(*, conn, secrets, secret_id: str, username: str) -> None:
    """Create/rotate a restricted role; publish credentials only after login succeeds.

    Requires that customer-sync schema migrations have already run. The operation
    is safe to re-run if Secrets Manager publication fails after the role update.
    """
    if not username or not username.replace("_", "").isalnum():
        raise ValueError("Invalid application database username")
    if username == os.environ.get("DB_ADMIN_USER"):
        raise ValueError("Application user must differ from administrator")

    apply_migrations(conn)
    for table in APP_TABLES:
        row = conn.execute("SELECT to_regclass(%s)", (f"public.{table}",)).fetchone()
        if not row or row[0] is None:
            raise RuntimeError("Required migration table could not be verified")

    generated = secrets.get_random_password(
        PasswordLength=40,
        ExcludePunctuation=True,
        RequireEachIncludedType=True,
    )["RandomPassword"]
    if not isinstance(generated, str) or len(generated) < 32:
        raise RuntimeError("Secret generation returned an invalid password")

    existing = conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (username,)).fetchone()
    role = sql.Identifier(username)
    password = sql.Literal(generated)
    if existing:
        conn.execute(sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}").format(role, password))
    else:
        conn.execute(sql.SQL("CREATE ROLE {} WITH LOGIN PASSWORD {}").format(role, password))

    conn.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
        sql.Identifier(os.environ["DB_NAME"]), role))
    conn.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
    # No DDL or privilege on unrelated application tables. The worker uses
    # read/write access only to the three customer identity/ledger tables.
    for table in APP_TABLES:
        conn.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {} TO {}").format(
            sql.Identifier("public", table), role))

    # Verify role connectivity before publication. A failure leaves the secret
    # unchanged. Re-running this one-off task will reconcile the password.
    with psycopg.connect(
        make_conninfo("", host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
                      dbname=os.environ["DB_NAME"], user=username, password=generated),
        sslmode="verify-full", sslrootcert=os.environ["DB_SSLROOTCERT"],
        connect_timeout=10,
    ) as check:
        if check.execute("SELECT current_user").fetchone()[0] != username:
            raise RuntimeError("Application database login verification failed")

    # AWS Secrets Manager does not create PostgreSQL users by itself.
    # A successful PutSecretValue creates an AWSCURRENT version.
    secrets.put_secret_value(
        SecretId=secret_id,
        SecretString=json.dumps({"username": username, "password": generated}),
    )


def main() -> int:
    required = ("DB_HOST", "DB_NAME", "DB_ADMIN_USER", "DB_ADMIN_PASSWORD",
                "DB_SSLROOTCERT", "APP_SECRET_ID", "DB_APP_USER", "AWS_REGION")
    missing = [key for key in required if not os.environ.get(key)]
    if missing:
        raise RuntimeError("Missing bootstrap environment configuration: " + ", ".join(missing))
    if not os.path.isfile(os.environ["DB_SSLROOTCERT"]):
        raise RuntimeError("Missing RDS CA certificate")

    with psycopg.connect(
        make_conninfo("", host=os.environ["DB_HOST"], port=os.environ.get("DB_PORT", "5432"),
                      dbname=os.environ["DB_NAME"], user=os.environ["DB_ADMIN_USER"],
                      password=os.environ["DB_ADMIN_PASSWORD"]),
        sslmode="verify-full", sslrootcert=os.environ["DB_SSLROOTCERT"],
        connect_timeout=10,
    ) as conn:
        # Commit DDL before attempting a second connection for verification.
        conn.autocommit = True
        secrets = boto3.client("secretsmanager", region_name=os.environ["AWS_REGION"])
        bootstrap(conn=conn, secrets=secrets, secret_id=os.environ["APP_SECRET_ID"],
                  username=os.environ["DB_APP_USER"])
    print("Application PostgreSQL role verified and credential secret populated")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Do not surface raw driver exceptions, which might carry connection info.
        print("Database bootstrap failed; inspect sanitized configuration and rerun", file=sys.stderr)
        sys.exit(1)
