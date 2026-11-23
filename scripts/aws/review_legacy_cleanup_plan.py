"""Fail-closed verification of an AWS Terraform legacy-resource retirement plan.

Read-only: accepts `terraform show -json reviewed.tfplan` output. It never
connects to AWS, edits state, or applies a plan. Prints only resource addresses.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# Every deletion requires a second, exact address match in the actual plan.
# Do not broaden to prefixes or glob patterns.
RETIRED = frozenset({
    "aws_cloudwatch_log_group.api",
    "aws_cloudwatch_log_group.consumer",
    "aws_cloudwatch_log_group.spark",
    "aws_cloudwatch_log_group.phase6_rds_exporter",
    "aws_ecr_repository.api",
    "aws_ecr_repository.consumer",
    "aws_ecr_repository.spark",
    "aws_ecr_repository.phase6_rds_exporter",
    "aws_iam_role.api_execution",
    "aws_iam_role.api_task",
    "aws_iam_role.consumer_task",
    "aws_iam_role.spark_task",
    "aws_iam_role.phase6_rds_exporter",
    "aws_iam_role_policy.api_execution_secret",
    "aws_iam_role_policy.api_publish",
    "aws_iam_role_policy.consumer",
    "aws_iam_role_policy.spark_s3",
    "aws_iam_role_policy.phase6_rds_exporter_s3",
    "aws_iam_role_policy_attachment.api_execution",
    "aws_security_group.alb",
    "aws_vpc_security_group_ingress_rule.alb_https",
    "aws_vpc_security_group_ingress_rule.ecs_from_alb",
    "aws_vpc_security_group_egress_rule.alb_to_ecs",
})
PROTECTED = frozenset({
    "aws_vpc.ecommerce",
    "aws_ecs_cluster.ecommerce",
    "aws_security_group.ecs",
    "aws_security_group.rds",
    "aws_db_instance.ecommerce",
    "aws_s3_bucket.landing",
    "aws_sqs_queue.events",
    "aws_sqs_queue.events_dlq",
})


class UnsafeCleanupPlan(ValueError):
    """Unexpected operations or incomplete plan."""


def review(plan: dict) -> list[str]:
    if not isinstance(plan.get("resource_changes"), list):
        raise UnsafeCleanupPlan("Missing resource_changes; provide a real Terraform plan JSON")
    if plan.get("errored"):
        raise UnsafeCleanupPlan("Terraform reported an errored plan")
    selected = []
    seen = set()
    for row in plan["resource_changes"]:
        address = row.get("address")
        actions = row.get("change", {}).get("actions", [])
        if not isinstance(address, str) or address in seen:
            raise UnsafeCleanupPlan("Missing or duplicate plan address")
        seen.add(address)
        if actions == ["no-op"]:
            continue
        if row.get("mode") == "data" and actions == ["read"]:
            continue
        # Imports, creates, replacements, updates, and deferred actions are
        # not part of this isolated retirement operation.
        if row.get("importing") or row.get("change", {}).get("importing"):
            raise UnsafeCleanupPlan("Unexpected import of " + address)
        if address not in RETIRED or actions != ["delete"]:
            raise UnsafeCleanupPlan(f"UNSAFE: {address} proposes {actions!r}")
        selected.append(address)
    if not selected:
        raise UnsafeCleanupPlan("No confirmed legacy deletions: do not use this gate for other plans")
    if PROTECTED.intersection(selected):
        raise UnsafeCleanupPlan("Critical active resource targeted")
    # Terraform may list planned imports separately.
    for imp in plan.get("resource_drift", []):
        if imp.get("change", {}).get("actions") not in (["no-op"], None):
            raise UnsafeCleanupPlan("Live state drift detected: review separately")
    return sorted(selected)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args()
    result = review(json.loads(args.plan.read_text(encoding="utf-8")))
    print(f"PASS: {len(result)} allowlisted legacy deletions, no other planned mutations")
    for address in result:
        print("DELETE", address)
    print("REVIEW REQUIRED: this check is necessary, not authorization to apply.")


if __name__ == "__main__":
    main()
