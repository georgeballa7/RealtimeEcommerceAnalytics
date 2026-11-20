"""Run dbt Core once on an ECS task using short-lived Redshift Serverless credentials.

The default action is intentionally inert. No credentials are logged or stored.
Every execution independently authenticates using the ECS task IAM role.
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

import boto3


PROJECT_DIR = Path("/opt/app/analytics/dbt")
DEFAULT_SELECT = ["+fct_refund_lines", "+fct_cancellations"]


def obtain_credentials(*, region: str, workgroup: str, database: str, client=None):
    if not workgroup or not database:
        raise ValueError("Redshift workgroup/database must be configured")
    if client is None:
        client = boto3.client("redshift-serverless", region_name=region)
    # The namespace's default database is analytics. Omitting dbName avoids
    # needing a separate IAM dbname resource grant; the SQL connection below
    # still explicitly verifies that it reached the analytics database.
    credentials = client.get_credentials(
        workgroupName=workgroup,
        durationSeconds=3600,
    )
    username = credentials.get("dbUser")
    password = credentials.get("dbPassword")
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        raise RuntimeError("Temporary Redshift credentials missing required fields")
    return username, password


def check_connection(*, host: str, database: str, username: str, password: str):
    """Create the IAMR database identity (first connection) without DDL."""
    import redshift_connector

    connection = redshift_connector.connect(
        host=host,
        port=5439,
        database=database,
        user=username,
        password=password,
        ssl=True,
        timeout=30,
    )
    try:
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT current_user, current_database()")
            row = cursor.fetchone()
            if not row or row[1] != database:
                raise RuntimeError("Connected to unexpected Redshift database")
        finally:
            cursor.close()
    finally:
        connection.close()
    print("Redshift IAM authentication check succeeded (no DDL or table changes).", flush=True)


def run_dbt(*, project: Path, username: str, password: str, action: str,
            selection: list[str], host: str, database: str):
    if action not in ("build", "debug"):
        raise ValueError("Only build or debug can execute dbt")
    args = ["dbt", action, "--project-dir", str(project), "--profiles-dir", str(project)]
    if action == "build":
        args += ["--select", *selection]
    env = dict(os.environ)
    env.update({
        "REDSHIFT_HOST": host,
        "REDSHIFT_USER": username,
        "REDSHIFT_PASSWORD": password,
        "REDSHIFT_DATABASE": database,
        "DBT_PROFILES_DIR": str(project),
    })
    # No shell command, credential arguments, or plaintext credential file.
    # Stay below the 3600s Redshift credential lifetime.
    subprocess.run(args, cwd=project, env=env, check=True, timeout=3300)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("help", "auth-check", "debug", "build"),
                        default="help")
    parser.add_argument("--select", nargs="+", default=DEFAULT_SELECT,
                        help="dbt model selection, only used with --action build")
    options = parser.parse_args(argv)
    if options.action == "help":
        parser.print_help()
        return 0

    region = os.environ.get("AWS_REGION", "")
    workgroup = os.environ.get("REDSHIFT_WORKGROUP", "")
    host = os.environ.get("REDSHIFT_HOST", "")
    database = os.environ.get("REDSHIFT_DATABASE", "")
    if region != "eu-central-1" or workgroup != "ecommerce-analytics-demo" or not host or database != "analytics":
        raise RuntimeError("Unexpected Redshift target. Refusing to run dbt.")
    username, password = obtain_credentials(
        region=region, workgroup=workgroup, database=database
    )
    if options.action == "auth-check":
        check_connection(
            host=host, database=database, username=username, password=password
        )
    else:
        run_dbt(
            project=PROJECT_DIR,
            username=username,
            password=password,
            action=options.action,
            selection=options.select,
            host=host,
            database=database,
        )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        print("dbt run failed or timed out; inspect task logs and warehouse state.", file=sys.stderr)
        sys.exit(1)
