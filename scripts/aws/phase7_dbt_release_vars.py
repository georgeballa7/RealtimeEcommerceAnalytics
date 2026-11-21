"""Generate Phase 7 dbt-only Terraform inputs from the *current* remote state.

Never uses Terraform defaults to decide whether existing optional workloads
should be destroyed. This program does not call AWS or apply anything.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# Imported both as a package in pytest and as a sibling script when the
# PowerShell rollout invokes this file from infrastructure/terraform.
if __package__:
    from .phase7_nat_cutover_plan import UnsafePlan, snapshot_vars
else:
    from phase7_nat_cutover_plan import UnsafePlan, snapshot_vars


def dbt_vars(state: dict, account: str, pinned_image: str, private_host: str = "") -> dict:
    if pinned_image and not re.fullmatch(
        rf"{re.escape(account)}\.dkr\.ecr\.eu-central-1\.amazonaws\.com/"
        r"ecommerce-phase6-dbt-demo@sha256:[a-f0-9]{64}",
        pinned_image,
    ):
        raise UnsafePlan("Unapproved dbt ECR image: must be immutable SHA-256")
    if private_host and not re.fullmatch(
        r"vpce-[a-z0-9-]+[.]vpce-svc-[a-z0-9-]+[.]eu-central-1[.]vpce[.]amazonaws[.]com",
        private_host,
    ):
        raise UnsafePlan("Unapproved dbt private Redshift endpoint hostname")
    if private_host and not pinned_image:
        raise UnsafePlan("A private dbt endpoint override requires a pinned dbt image")
    cfg = snapshot_vars(state, account)
    if cfg.get("nat_gateway_mode") != "on_demand":
        raise UnsafePlan("Unexpected state snapshot contract")
    # This is a dbt bootstrap, NOT the NAT cutover.
    cfg["nat_gateway_mode"] = "static"
    cfg["nat_gateway_count"] = 1
    for flag in (
        "enable_redshift_warehouse",
        "enable_phase6_glue_silver",
        "enable_phase6_glue_rds_lease",
        "enable_phase5_legacy_lifecycle_schedule",
    ):
        if cfg.get(flag) is not True:
            raise UnsafePlan("Refusing dbt bootstrap; required live service absent: " + flag)
    cfg["enable_phase6_dbt_runner"] = True
    cfg["phase6_dbt_image"] = pinned_image
    if private_host:
        cfg["phase6_dbt_private_host"] = private_host
    return cfg


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--state", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--account", type=str, required=True)
    p.add_argument("--pinned-image", type=str, default="")
    p.add_argument("--private-host", type=str, default="")
    a = p.parse_args()
    try:
        if not re.fullmatch(r"[0-9]{12}", a.account):
            raise UnsafePlan("Unexpected AWS account ID")
        state = json.loads(a.state.read_text(encoding="utf-8-sig"))
        variables = dbt_vars(state, a.account, a.pinned_image, a.private_host)
        a.output.write_text(json.dumps(variables, indent=2) + "\n", encoding="utf-8")
        print("Preserved state-derived image pins and opted-in resources. No AWS changes.")
    except (UnsafePlan, KeyError, ValueError, TypeError) as exc:
        p.exit(2, "Refusing dbt bootstrap vars: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
