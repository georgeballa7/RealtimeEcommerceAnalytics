"""Offline safety-contract tests for the guarded on-demand NAT Terraform plan."""
import json

import pytest

from scripts.aws import phase7_nat_cutover_plan as plan


PINNED = (
    "825414820081.dkr.ecr.eu-central-1.amazonaws.com/test-image@sha256:"
    + "a" * 64
)


def resource(address, **values):
    return {"address": address, "values": values}


def sample_state():
    return {
        "values": {"root_module": {"resources": [
            resource("aws_nat_gateway.ecommerce[0]", id="nat-static"),
            resource("aws_eip.nat[0]", id="eipalloc-1"),
            resource("aws_route.application[0]", nat_gateway_id="nat-static"),
            resource("aws_route.application[1]", nat_gateway_id="nat-static"),
            resource("aws_vpc_endpoint.s3", id="vpce-1"),
            resource("aws_ecs_cluster.ecommerce", name="ecommerce-demo"),
            resource("aws_subnet.public[0]", cidr_block="10.20.0.0/24"),
            resource("aws_subnet.public[1]", cidr_block="10.20.1.0/24"),
            resource("aws_subnet.application[0]", cidr_block="10.20.20.0/24"),
            resource("aws_subnet.application[1]", cidr_block="10.20.21.0/24"),
            resource("aws_subnet.database[0]", cidr_block="10.20.10.0/24"),
            resource("aws_subnet.database[1]", cidr_block="10.20.11.0/24"),
            resource("aws_vpc.ecommerce", cidr_block="10.20.0.0/16"),
            resource("aws_db_instance.ecommerce",
                     instance_class="db.t4g.micro", allocated_storage=20,
                     multi_az=False, db_name="ecommerce_db",
                     username="ecommerce_admin"),
            resource("aws_lambda_function.phase5_fastapi[0]",
                     image_uri=PINNED, environment=[{"variables": {
                         "SHOPIFY_SHOP_DOMAIN": "example.myshopify.com",
                         "SHOPIFY_WEBHOOK_SECRET_ARN": "arn:aws:secretsmanager:example",
                     }}]),
            resource("aws_lambda_function.phase5_landing[0]",
                     image_uri=PINNED),
            resource("aws_lambda_function.phase5_controller[0]",
                     image_uri=PINNED),
            resource("aws_ecs_task_definition.phase5_generator[0]",
                     container_definitions=json.dumps([{
                         "name": "phase5-generator", "image": PINNED,
                         "secrets": [{"name": "SHOPIFY_CLIENT_ID",
                                      "valueFrom": "arn:aws:secretsmanager:client:client_id::"}]
                     }])),
            resource("aws_ecs_task_definition.phase5_projector[0]",
                     container_definitions=json.dumps([{
                         "name": "phase5-projector", "image": PINNED
                     }])),
            resource("aws_ecs_task_definition.phase5_lifecycle[0]",
                     container_definitions=json.dumps([{
                         "name": "phase5-lifecycle", "image": PINNED
                     }])),
            resource("aws_scheduler_schedule.phase5_lifecycle[0]", id="lifecycle"),
            resource("aws_redshiftserverless_workgroup.analytics[0]",
                     workgroup_name="ecommerce-analytics-demo"),
            resource("aws_glue_job.phase6_shopify_silver[0]", name="shopify-silver"),
            resource("aws_iam_role_policy.phase6_controller_glue_inspect[0]",
                     id="policy"),
        ]}},
    }


def valid_plan():
    changes = [
        {"address": addr, "mode": "managed",
         "change": {"actions": ["delete"]}}
        for addr in sorted(plan.EXPECTED_DELETES)
    ]
    changes += [
        {"address": addr, "mode": "managed",
         "change": {"actions": ["create"]}}
        for addr in sorted(plan.EXPECTED_CREATES)
    ]
    changes += [
        {"address": "aws_vpc.ecommerce", "mode": "managed",
         "change": {"actions": ["no-op"]}},
        {"address": "data.archive_file.phase6_nat_lifecycle[0]",
         "mode": "data", "change": {"actions": ["read"]}},
    ]
    return {"resource_changes": changes}


def test_snapshot_preserves_live_images_and_enabled_warehouse_components():
    result = plan.snapshot_vars(sample_state(), "825414820081")
    assert result["nat_gateway_mode"] == "on_demand"
    assert result["nat_gateway_count"] == 1
    assert result["phase5_lambda_image"] == PINNED
    assert result["phase5_generator_image"] == PINNED
    assert result["phase5_projector_image"] == PINNED
    assert result["phase5_lifecycle_image"] == PINNED
    assert result["shopify_client_credentials_secret_arn"] == "arn:aws:secretsmanager:client"
    assert result["enable_redshift_warehouse"] is True
    assert result["enable_phase6_glue_silver"] is True
    assert result["enable_phase6_glue_rds_lease"] is True
    assert result["enable_phase5_legacy_lifecycle_schedule"] is True
    assert result["enable_phase6_dbt_runner"] is False
    assert result["private_subnet_cidrs"] == ["10.20.10.0/24", "10.20.11.0/24"]


def test_snapshot_refuses_nonmatching_lambda_images_and_missing_static_route():
    state = sample_state()
    state["values"]["root_module"]["resources"][15]["values"]["image_uri"] = (
        PINNED[:-1] + "b"
    )
    with pytest.raises(plan.UnsafePlan, match="Lambda images"):
        plan.snapshot_vars(state, "825414820081")
    state = sample_state()
    state["values"]["root_module"]["resources"] = [
        r for r in state["values"]["root_module"]["resources"]
        if r["address"] != "aws_route.application[1]"
    ]
    with pytest.raises(plan.UnsafePlan, match="static NAT"):
        plan.snapshot_vars(state, "825414820081")


def test_plan_review_accepts_only_exact_nat_cutover():
    result = plan.review_plan(valid_plan())
    assert result["decision"] == "PLAN_ONLY_REVIEW_REQUIRED"
    assert result["nat_gateway_deletes"] == 1
    assert result["application_default_route_deletes"] == 2
    assert result["new_nat_lifecycle_resources"] == 8
    assert result["apply_permitted_by_this_tool"] is False


@pytest.mark.parametrize("extra", [
    {"address": "aws_db_instance.ecommerce", "mode": "managed",
     "change": {"actions": ["delete"]}},
    {"address": "aws_lambda_function.phase5_controller[0]", "mode": "managed",
     "change": {"actions": ["update"]}},
    {"address": "aws_nat_gateway.ecommerce[0]", "mode": "managed",
     "change": {"actions": ["delete", "create"]}},
])
def test_plan_review_rejects_unrelated_or_replacement_changes(extra):
    candidate = valid_plan()
    candidate["resource_changes"].append(extra)
    with pytest.raises(plan.UnsafePlan):
        plan.review_plan(candidate)


def test_plan_review_refuses_missing_route_delete_or_missing_lambda():
    for addr in ["aws_route.application[1]",
                 "aws_lambda_function.phase6_nat_lifecycle[0]"]:
        candidate = valid_plan()
        candidate["resource_changes"] = [
            r for r in candidate["resource_changes"] if r["address"] != addr
        ]
        with pytest.raises(plan.UnsafePlan, match="missing"):
            plan.review_plan(candidate)


def test_plan_review_rejects_unexpected_data_reads():
    candidate = valid_plan()
    candidate["resource_changes"].append({
        "address": "data.external.unexpected", "mode": "data",
        "change": {"actions": ["read"]},
    })
    with pytest.raises(plan.UnsafePlan, match="data-source"):
        plan.review_plan(candidate)


def test_dbt_release_preserves_optional_db_bootstrap_and_static_nat():
    from scripts.aws.phase7_dbt_release_vars import dbt_vars

    state = sample_state()
    state["values"]["root_module"]["resources"].append(
        resource(
            "aws_ecs_task_definition.db_bootstrap[0]",
            container_definitions=json.dumps([{
                "name": "bootstrap", "image": PINNED,
            }]),
        )
    )
    config = dbt_vars(state, "825414820081", "")
    assert config["nat_gateway_mode"] == "static"
    assert config["enable_phase6_dbt_runner"] is True
    assert config["phase6_dbt_image"] == ""
    assert config["db_bootstrap_image"] == PINNED
    assert config["enable_redshift_warehouse"] is True


def test_dbt_release_refuses_unapproved_docker_image():
    from scripts.aws.phase7_dbt_release_vars import dbt_vars

    image = "123456789012.dkr.ecr.eu-central-1.amazonaws.com/ecommerce-phase6-dbt-demo@sha256:" + "a" * 64
    with pytest.raises(plan.UnsafePlan, match="Unapproved dbt ECR image"):
        dbt_vars(sample_state(), "825414820081", image)


def test_state_helpers_work_from_terraform_directory_as_native_cli(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    state = tmp_path / "terraform-show.json"
    output = tmp_path / "release-vars.json"
    state.write_text(json.dumps(sample_state()), encoding="utf-8")
    tool = root / "scripts" / "aws" / "phase7_dbt_release_vars.py"
    run = subprocess.run(
        [
            sys.executable, "-X", "utf8", str(tool),
            "--state", str(state), "--output", str(output),
            "--account", "825414820081",
        ],
        cwd=root / "infrastructure" / "terraform",
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert run.returncode == 0, run.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["nat_gateway_mode"] == "static"
    assert payload["enable_phase6_dbt_runner"] is True
