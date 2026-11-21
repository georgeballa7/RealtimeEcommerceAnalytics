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
    assert "for ($attempt = 0; $attempt -lt 165; $attempt++)" in launch
    assert "dbt ECS task has not stopped within 55 minutes" in launch
    assert "& aws ecs wait tasks-stopped" not in launch
    assert "phase7_dbt_release_vars.py" in deploy
    assert "Preserve existing Terraform resources" in deploy




def test_deployed_vpc_az_provider_lookup_is_read_only_and_allowed():
    candidate = changes(guard.FOUNDATION)
    candidate["resource_changes"].append({
        "address": "data.aws_availability_zones.available",
        "mode": "data", "change": {"actions": ["read"]},
    })
    assert guard.validate(candidate, "foundation")["create_count"] == 8

def test_phase7_dbt_foundation_omits_empty_optional_image_argument():
    """First-stage PowerShell must not pass an empty --pinned-image to argparse."""
    deploy = (ROOT / "scripts/aws/deploy_phase6_dbt.ps1").read_text()
    assert "$builderArgs = @('-X', 'utf8', $VarsBuilder" in deploy
    assert "if ($PinnedImage -ne '') {" in deploy
    assert "$builderArgs += @('--pinned-image', $PinnedImage)" in deploy
    assert "& python @builderArgs" in deploy
    assert "--account $AccountId --pinned-image $PinnedImage" not in deploy


def test_ecs_runner_decodes_subnet_json_as_flat_strings_for_windows_powershell():
    """Prevent 2 subnet IDs being wrapped as a single JSON-array pipeline object."""
    launch = (ROOT / "scripts/aws/run_phase6_dbt.ps1").read_text()
    assert "ConvertFrom-Json -InputObject (($SubnetsJson | Out-String).Trim())" in launch
    assert "[string[]]$Subnets = $SubnetValues" in launch
    assert "$SubnetValues -isnot [System.Array]" in launch
    assert "$Subnets | Select-Object -Unique" in launch
    assert "subnets = $Subnets" in launch
    assert "@($SubnetsJson | ConvertFrom-Json)" not in launch
