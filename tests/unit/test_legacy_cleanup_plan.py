"""Unit tests for fail-closed Terraform legacy cleanup review."""
import pytest

from scripts.aws.review_legacy_cleanup_plan import UnsafeCleanupPlan, review


def plan(*changes):
    return {"resource_changes": [
        {"address": a, "mode": "managed", "change": {"actions": action}}
        for a, action in changes
    ]}


def test_accepts_exact_legacy_destroy_only():
    assert review(plan(
        ("aws_security_group.alb", ["delete"]),
        ("aws_security_group.ecs", ["no-op"]),
    )) == ["aws_security_group.alb"]


@pytest.mark.parametrize("address,actions", [
    ("aws_security_group.ecs", ["delete"]),
    ("aws_db_instance.ecommerce", ["update"]),
    ("aws_redshiftserverless_workgroup.analytics[0]", ["delete"]),
    ("aws_ecr_repository.api", ["delete", "create"]),
    ("aws_security_group.alb", ["create"]),
    ("aws_iam_role.phase7_dbt_orchestrator", ["create"]),
])
def test_rejects_unexpected_change(address, actions):
    with pytest.raises(UnsafeCleanupPlan, match="UNSAFE"):
        review(plan((address, actions)))


def test_rejects_empty_plan():
    with pytest.raises(UnsafeCleanupPlan, match="No confirmed"):
        review(plan())


def test_rejects_duplicate_addresses():
    with pytest.raises(UnsafeCleanupPlan, match="duplicate"):
        review(plan(
            ("aws_security_group.alb", ["delete"]),
            ("aws_security_group.alb", ["delete"]),
        ))


def test_import_is_rejected():
    sample = plan(("aws_security_group.alb", ["delete"]))
    sample["resource_changes"].append({
        "address": "aws_iam_role.phase7_dbt_orchestrator",
        "mode": "managed",
        "importing": {"id": "existing"},
        "change": {"actions": ["no-op"]},
    })
    with pytest.raises(UnsafeCleanupPlan, match="import"):
        review(sample)
