"""Offline tests for the first real dbt ECS foundation, task and full mart build."""
from pathlib import Path

import pytest

from scripts.aws import phase7_dbt_plan_guard as guard

ROOT = Path(__file__).resolve().parents[2]


def changes(addrs):
    return {"resource_changes": [
        {"address": name, "mode": "managed", "change": {"actions": ["create"]}}
        for name in sorted(addrs)
    ]}


def test_dbt_foundation_restricts_creation_to_eight_optional_components():
    decision = guard.validate(changes(guard.FOUNDATION), "foundation")
    assert decision["decision"] == "REVIEWED_DBT_ONLY"
    assert decision["create_count"] == 8
    assert decision["delete_count"] == 0


def test_dbt_task_second_stage_creates_only_one_task_definition():
    decision = guard.validate(changes(guard.TASK), "task")
    assert decision["create_count"] == 1
    assert decision["update_count"] == 0


@pytest.mark.parametrize("address,action", [
    ("aws_db_instance.ecommerce", ["delete"]),
    ("aws_nat_gateway.ecommerce[0]", ["delete"]),
    ("aws_lambda_function.phase5_controller[0]", ["update"]),
    ("aws_redshiftserverless_workgroup.analytics[0]", ["update"]),
    ("aws_ecs_task_definition.phase5_generator[0]", ["delete", "create"]),
])
def test_rejects_unrelated_aws_changes(address, action):
    data = changes(guard.FOUNDATION)
    data["resource_changes"].append({
        "address": address, "mode": "managed", "change": {"actions": action}
    })
    with pytest.raises(guard.UnsafeDbtPlan, match="Refusing"):
        guard.validate(data, "foundation")


def test_rejects_missing_one_or_unknown_data_read():
    data = changes(guard.TASK)
    data["resource_changes"] = []
    with pytest.raises(guard.UnsafeDbtPlan):
        guard.validate(data, "task")

    data = changes(guard.TASK)
    data["resource_changes"].append({
        "address": "data.aws_iam_policy_document.unknown",
        "mode": "data", "change": {"actions": ["read"]},
    })
    with pytest.raises(guard.UnsafeDbtPlan):
        guard.validate(data, "task")


def test_release_scripts_use_phase7_branch_and_exact_plan_gate():
    deploy = (ROOT / "scripts/aws/deploy_phase6_dbt.ps1").read_text()
    publish = (ROOT / "scripts/aws/publish_phase6_dbt_image.ps1").read_text()
    launch = (ROOT / "scripts/aws/run_phase6_dbt.ps1").read_text()

    assert "feature/dbt-analytics-churn-model" in deploy
    assert "feature/dbt-analytics-churn-model" in publish
    assert "phase7_dbt_plan_guard.py" in deploy
    assert "Strict dbt-only Terraform resource review" in deploy
    assert "& terraform apply -input=false $PlanPath" in deploy
    assert "$Scope = 'Core'" in launch
    assert "'--action', 'build', '--select', '*'" in launch
    assert "if (-not $Execute)" in launch
    assert "assignPublicIp = 'DISABLED'" in launch


def test_dbt_release_vars_preserves_state_and_static_nat():
    from scripts.aws.phase7_dbt_release_vars import dbt_vars
    from tests.unit.test_phase7_nat_cutover_plan import sample_state, PINNED

    state = sample_state()
    state["values"]["root_module"]["resources"].append({
        "address": "aws_ecs_task_definition.db_bootstrap[0]",
        "values": {"container_definitions": __import__("json").dumps([
            {"name": "bootstrap", "image": PINNED}
        ])},
    })
    result = dbt_vars(state, "825414820081", "")
    assert result["nat_gateway_mode"] == "static"
    assert result["enable_phase6_dbt_runner"] is True
    assert result["phase6_dbt_image"] == ""
    assert result["db_bootstrap_image"] == PINNED
    assert result["enable_redshift_warehouse"] is True


def test_dbt_release_vars_rejects_wrong_account_image():
    from scripts.aws.phase7_dbt_release_vars import dbt_vars
    from tests.unit.test_phase7_nat_cutover_plan import sample_state

    image = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/ecommerce-phase6-dbt-demo@sha256:" + "a" * 64
    with pytest.raises(ValueError, match="Unapproved dbt ECR image"):
        dbt_vars(sample_state(), "825414820081", image)
