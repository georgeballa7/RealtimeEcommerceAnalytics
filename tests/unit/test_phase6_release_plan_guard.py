"""Phase 6 release safety: provider JSON reordering is harmless, drift is not."""
from __future__ import annotations

import copy
import json

import pytest

from scripts.aws.validate_phase6_release_plan import (
    GENERATOR, JOB, LAMBDA, POLICY, SCHEDULES, UnsafePlan, validate_plan,
)


OLD_LAMBDA = "repo/lambda@sha256:" + "a" * 64
NEW_LAMBDA = "repo/lambda@sha256:" + "b" * 64
OLD_GENERATOR = "repo/generator@sha256:" + "c" * 64
NEW_GENERATOR = "repo/generator@sha256:" + "d" * 64
TASK = "arn:aws:ecs:eu-central-1:825414820081:task-definition/ecommerce-phase5-generator-demo:4"


def fixture_plan():
    old_c = {
        "name": "phase5-generator", "image": OLD_GENERATOR,
        "environment": [
            {"name": "S3_LANDING_BUCKET", "value": "bucket"},
            {"name": "AWS_REGION", "value": "eu-central-1"},
        ],
        "secrets": [
            {"name": "SHOPIFY_CLIENT_SECRET", "valueFrom": "secret2"},
            {"name": "SHOPIFY_CLIENT_ID", "valueFrom": "secret1"},
        ],
        "essential": True,
        "command": ["--help"],
        "logConfiguration": {"logDriver": "awslogs", "options": {"awslogs-group": "logs"}},
    }
    new_c = {
        "logConfiguration": {"options": {"awslogs-group": "logs"}, "logDriver": "awslogs"},
        "command": ["--help"], "essential": True,
        "secrets": list(reversed(old_c["secrets"])),
        "environment": list(reversed(old_c["environment"])),
        "image": NEW_GENERATOR, "name": "phase5-generator",
    }
    old_task = {"arn": TASK, "revision": 4, "id": TASK, "family": "ecommerce-phase5-generator-demo", "cpu": "512", "container_definitions": json.dumps([old_c])}
    new_task = {"arn": None, "revision": None, "id": None, "family": "ecommerce-phase5-generator-demo", "cpu": "512", "container_definitions": json.dumps([new_c])}
    changes = [{
        "address": GENERATOR,
        "change": {"actions": ["delete", "create"], "before": old_task, "after": new_task},
    }]
    for address in sorted(LAMBDA):
        old = {"image_uri": OLD_LAMBDA, "memory_size": 512, "last_modified": "old",
               "environment": [{"variables": {"REGION": "eu-central-1"}}]}
        new = {"image_uri": NEW_LAMBDA, "memory_size": 512, "last_modified": None,
               "environment": [{"variables": {"REGION": "eu-central-1"}}]}
        if address.endswith("phase5_controller[0]"):
            new["environment"][0]["variables"]["PHASE6_GLUE_JOB_NAME"] = JOB
        changes.append({"address": address, "change": {"actions": ["update"], "before": old, "after": new}})
    for address in sorted(SCHEDULES):
        old_schedule = {
            "name": address, "schedule_expression": "cron(0 8 * * ? *)",
            "schedule_expression_timezone": "Europe/Berlin",
            "target": [{"ecs_parameters": [{"task_definition_arn": TASK, "task_count": 1}], "role_arn": "scheduler-role"}],
        }
        new_schedule = copy.deepcopy(old_schedule)
        new_schedule["target"][0]["ecs_parameters"][0]["task_definition_arn"] = None
        changes.append({"address": address, "change": {"actions": ["update"], "before": old_schedule, "after": new_schedule}})
    policy = {
        "name": "phase6-read-glue-status-for-safe-rds-stop",
        "role": "ecommerce-phase5-controller-demo",
        "policy": json.dumps({"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["glue:GetJobRun"], "Resource": "arn:aws:glue:eu-central-1:825414820081:job/" + JOB}
        ]}),
    }
    changes.append({"address": POLICY, "change": {"actions": ["create"], "before": None, "after": policy}})
    return {"resource_changes": changes}


def verify(plan):
    return validate_plan(
        plan, previous_lambda_image=OLD_LAMBDA, new_lambda_image=NEW_LAMBDA,
        previous_generator_image=OLD_GENERATOR, new_generator_image=NEW_GENERATOR,
        generator_task=TASK,
    )


def test_accepts_only_intended_release_with_shuffled_json_keys_and_environment_order():
    assert len(verify(fixture_plan())) == 7


@pytest.mark.parametrize("change_path", ["env_value", "secret", "command", "log", "cpu", "cron", "timezone", "memory", "iam"])
def test_fails_closed_for_real_drift(change_path):
    plan = fixture_plan()
    task = plan["resource_changes"][0]["change"]
    if change_path in {"env_value", "secret", "command", "log"}:
        container = json.loads(task["after"]["container_definitions"])
        obj = container[0]
        if change_path == "env_value":
            obj["environment"][0]["value"] = "wrong-bucket"
        if change_path == "secret":
            obj["secrets"][0]["valueFrom"] = "wrong-secret"
        if change_path == "command":
            obj["command"] = ["--apply"]
        if change_path == "log":
            obj["logConfiguration"]["options"]["awslogs-group"] = "other"
        task["after"]["container_definitions"] = json.dumps(container)
    elif change_path == "cpu":
        task["after"]["cpu"] = "2048"
    elif change_path in {"cron", "timezone"}:
        ch = next(c["change"] for c in plan["resource_changes"] if c["address"] in SCHEDULES)
        field = "schedule_expression" if change_path == "cron" else "schedule_expression_timezone"
        ch["after"][field] = "different"
    elif change_path == "memory":
        ch = next(c["change"] for c in plan["resource_changes"] if c["address"] in LAMBDA)
        ch["after"]["memory_size"] = 1024
    elif change_path == "iam":
        ch = next(c["change"] for c in plan["resource_changes"] if c["address"] == POLICY)
        p = json.loads(ch["after"]["policy"])
        p["Statement"][0]["Action"] = ["glue:*"]
        ch["after"]["policy"] = json.dumps(p)
    with pytest.raises(UnsafePlan):
        verify(plan)


def test_fails_for_unexpected_resources():
    plan = fixture_plan()
    plan["resource_changes"].append({
        "address": "aws_db_instance.ecommerce",
        "change": {"actions": ["delete"], "before": {}, "after": None},
    })
    with pytest.raises(UnsafePlan):
        verify(plan)


def test_fails_for_unexpected_generator_image():
    plan = fixture_plan()
    container = json.loads(plan["resource_changes"][0]["change"]["after"]["container_definitions"])
    container[0]["image"] = "unexpected:latest"
    plan["resource_changes"][0]["change"]["after"]["container_definitions"] = json.dumps(container)
    with pytest.raises(UnsafePlan):
        verify(plan)
