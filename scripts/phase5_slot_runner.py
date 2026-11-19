"""Build one deterministic daily scheduled Shopify generator run ID.

Use as ECS image ENTRYPOINT. The existing generator script remains the source
of order creation and S3 idempotency. Preview unless --apply is explicitly set.
"""
from __future__ import annotations

import argparse
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
    count = 20 if slot == "morning" else 25
    if smoke_run_id is not None:
        if not re.fullmatch(r"smoke-[a-z0-9-]{4,58}", smoke_run_id):
            raise ValueError("Unsafe smoke run ID")
        run_id, count = smoke_run_id, 1
    path = str(Path(__file__).with_name("generate_shopify_live_orders.py"))
    cmd = [sys.executable, path, "--run-id", run_id, "--count", str(count)]
    if apply:
        cmd += ["--apply", "--confirm-run-id", run_id]
    return cmd


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--slot", choices=("morning", "afternoon"), required=True)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--smoke-run-id")
    args = ap.parse_args()
    # Host controls the task; never infer an apply from the environment.
    cmd = build_command(args.slot, datetime.now(ZoneInfo("UTC")), apply=args.apply, smoke_run_id=args.smoke_run_id)
    raise SystemExit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
