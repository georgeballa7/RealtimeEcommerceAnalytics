"""Strictly review an existing Terraform plan for the first dbt ECS deployment.

This is an allowlist ONLY; it never runs Terraform or changes AWS.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


class UnsafeDbtPlan(ValueError):
    pass


FOUNDATION = {
    "aws_ecr_repository.phase6_dbt[0]",
    "aws_cloudwatch_log_group.phase6_dbt[0]",
    "aws_iam_role.phase6_dbt_task[0]",
    "aws_iam_role_policy.phase6_dbt_credentials[0]",
    "aws_security_group.phase6_dbt[0]",
    "aws_vpc_security_group_egress_rule.phase6_dbt_redshift[0]",
    "aws_vpc_security_group_egress_rule.phase6_dbt_https[0]",
    "aws_vpc_security_group_ingress_rule.redshift_from_phase6_dbt[0]",
}
TASK = {"aws_ecs_task_definition.phase6_dbt[0]"}


def validate(plan: dict, stage: str) -> dict:
    if stage not in ("foundation", "task"):
        raise UnsafeDbtPlan("Invalid stage")
    rows = plan.get("resource_changes")
    if not isinstance(rows, list):
        raise UnsafeDbtPlan("Plan has no resource_changes")
    modified = {}
    for row in rows:
        addr = row.get("address")
        actions = row.get("change", {}).get("actions")
        if actions == ["no-op"]:
            continue
        # The existing VPC AZ lookup may be refreshed during planning.
        # This is a provider data read, not a new infrastructure resource.
        if (row.get("mode") == "data"
                and addr == "data.aws_availability_zones.available"
                and actions == ["read"]):
            continue
        # Provider data sources often re-read during plan, but any new
        # unknown read is treated as a blocker for a minimal dbt deployment.
        if addr in modified:
            raise UnsafeDbtPlan("Duplicate change address: " + str(addr))
        modified[addr] = actions
    expected = FOUNDATION if stage == "foundation" else TASK
    # ECS task definitions are immutable: changing only the pinned dbt image
    # registers a new revision (Terraform replace). Permit that exact
    # address only, never replacement of NAT, Redshift or other ECS tasks.
    task_actions = [["create"], ["delete", "create"], ["create", "delete"]]
    task_change = modified.get("aws_ecs_task_definition.phase6_dbt[0]")
    accepted = (
        modified == {name: ["create"] for name in expected}
        if stage == "foundation"
        else set(modified) == TASK and task_change in task_actions
    )
    if not accepted:
        missing = sorted(expected - set(modified))
        extra = {key: val for key, val in modified.items()
                 if key not in expected or (
                     stage == "foundation" and val != ["create"]) or (
                     stage == "task" and val not in task_actions)}
        raise UnsafeDbtPlan(
            "Refusing unrelated, incomplete or destructive Terraform diff. "
            + "Missing: " + json.dumps(missing)
            + "; unexpected: " + json.dumps(extra, sort_keys=True)
        )
    replaced = stage == "task" and len(task_change) == 2
    return {
        "decision": "REVIEWED_DBT_ONLY",
        "stage": stage,
        "create_count": len(expected),
        "delete_count": 1 if replaced else 0,
        "replace_count": 1 if replaced else 0,
        "update_count": 0,
        "allowed": sorted(expected),
        "note": "A user must explicitly approve the saved Terraform plan.",
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--stage", choices=("foundation", "task"), required=True)
    args = parser.parse_args()
    try:
        parsed = json.loads(args.plan.read_text(encoding="utf-8-sig"))
        print(json.dumps(validate(parsed, args.stage), indent=2))
    except (ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"UNSAFE dbt Terraform plan: {exc}\n")


if __name__ == "__main__":
    main()
