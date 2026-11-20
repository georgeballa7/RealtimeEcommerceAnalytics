"""Build one deterministic daily scheduled Shopify generator run ID.

Use as ECS image ENTRYPOINT. The existing generator script remains the source
of order creation and S3 idempotency. Preview unless --apply is explicitly set.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


def build_command(slot: str, now: datetime, apply: bool = False, smoke_run_id: str | None = None) -> list[str]:
    if slot not in {"morning", "afternoon"}:
        raise ValueError("Invalid slot")
    local = now.astimezone(ZoneInfo("Europe/Berlin"))
    run_id = f"live-{local:%Y%m%d}-{slot}"
    count = 100 if slot == "morning" else 150
    if smoke_run_id is not None:
        if not re.fullmatch(r"smoke-[a-z0-9-]{4,58}", smoke_run_id):
            raise ValueError("Unsafe smoke run ID")
        run_id, count = smoke_run_id, 1
    path = str(Path(__file__).with_name("generate_shopify_live_orders.py"))
    cmd = [sys.executable, path, "--run-id", run_id, "--count", str(count)]
    if apply:
        cmd += ["--apply", "--confirm-run-id", run_id]
    return cmd


def assert_deployment_ready():
    table_name = os.environ.get("PHASE5_CONTROLLER_TABLE")
    if not table_name:
        return  # Local preview/test invocation (not ECS scheduled production)
    import boto3
    record = boto3.resource("dynamodb").Table(table_name).get_item(
        Key={"id":"phase5-controller"}, ConsistentRead=True).get("Item", {})
    if not (record.get("schema_ready") is True and record.get("webhooks_ready") is True):
        raise RuntimeError("Phase 5 deployment is not ready for Shopify mutations")



def publish_completed_slot(bucket: str, run_id: str, slot: str) -> None:
    """S3 commit marker written LAST, after generator and lifecycle both succeed."""
    import boto3
    from botocore.exceptions import ClientError

    key = f"simulation/phase5/slots/v1/{run_id}.json"
    data = {"schema_version": 1, "run_id": run_id, "slot": slot,
            "generation": "complete", "lifecycle": "settled"}
    s3 = boto3.client("s3")
    try:
        s3.put_object(Bucket=bucket, Key=key, IfNoneMatch="*",
                      Body=json.dumps(data, sort_keys=True).encode(),
                      ContentType="application/json")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") not in {"PreconditionFailed", "412"}:
            raise
        existing = json.loads(s3.get_object(Bucket=bucket, Key=key)["Body"].read())
        if existing != data:
            raise RuntimeError("Existing slot completion marker conflicts with current run") from exc


def run_combined_slot(slot: str, now: datetime, *, apply: bool,
                      smoke_run_id: str | None = None) -> int:
    """Always execute generator first; lifecycle must settle before slot commit."""
    cmd = build_command(slot, now, apply=apply, smoke_run_id=smoke_run_id)
    code = subprocess.call(cmd)
    if code or not apply or smoke_run_id:
        return code
    lifecycle = str(Path(__file__).with_name("run_phase5_lifecycle.py"))
    code = subprocess.call([sys.executable, lifecycle, "--apply",
                            "--confirm-apply", "--require-settled", "--limit", "500"])
    if code:
        return code
    run_id = f"live-{now.astimezone(ZoneInfo('Europe/Berlin')):%Y%m%d}-{slot}"
    bucket = os.environ.get("S3_LANDING_BUCKET")
    if not bucket:
        raise RuntimeError("Cannot publish daily slot readiness without S3 bucket")
    publish_completed_slot(bucket, run_id, slot)
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slot", choices=("morning", "afternoon"), required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--smoke-run-id")
    args = ap.parse_args()
    # Host controls the task; never infer an apply from the environment.
    if args.apply:
        assert_deployment_ready()
    code = run_combined_slot(args.slot, datetime.now(ZoneInfo("UTC")),
                             apply=args.apply, smoke_run_id=args.smoke_run_id)
    raise SystemExit(code)


if __name__ == "__main__":
    main()
