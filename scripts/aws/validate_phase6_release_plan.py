"""Fail-closed, semantic guard for the Phase 6 Terraform release plan.

JSON object key order is not significant. ECS environment/secrets entry order
is not significant either. Every OTHER unapproved change is rejected, with
safe field paths rather than potentially sensitive before/after values.
"""
from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path


LAMBDA = {
    "aws_lambda_function.phase5_fastapi[0]",
    "aws_lambda_function.phase5_landing[0]",
    "aws_lambda_function.phase5_controller[0]",
}
GENERATOR = "aws_ecs_task_definition.phase5_generator[0]"
SCHEDULES = {
    'aws_scheduler_schedule.phase5_generation["morning"]',
    'aws_scheduler_schedule.phase5_generation["afternoon"]',
}
POLICY = "aws_iam_role_policy.phase6_controller_glue_inspect[0]"
ALLOWED = LAMBDA | {GENERATOR, POLICY} | SCHEDULES
JOB = "ecommerce-phase6-shopify-silver-demo"
# Terraform creates new read-only provider outputs for image/task updates.
LAMBDA_COMPUTED = {
    "arn", "id", "invoke_arn", "qualified_arn", "qualified_invoke_arn",
    "version", "last_modified", "source_code_hash", "signing_job_arn",
    "signing_profile_version_arn", "tags_all",
}
TASK_COMPUTED = {"arn", "id", "revision", "tags_all"}


class UnsafePlan(ValueError):
    pass


def differences(left, right, path=""):
    """Return only changed key paths; never disclose secret values."""
    if isinstance(left, dict) and isinstance(right, dict):
        result = []
        for key in sorted(set(left) | set(right)):
            new_path = f"{path}.{key}" if path else key
            if key not in left or key not in right:
                result.append(new_path)
            else:
                result.extend(differences(left[key], right[key], new_path))
        return result
    if isinstance(left, list) and isinstance(right, list):
        if len(left) != len(right):
            return [f"{path}.length"]
        result = []
        for i, (a, b) in enumerate(zip(left, right)):
            result.extend(differences(a, b, f"{path}[{i}]"))
        return result
    return [] if left == right else [path]


def require_equal(left, right, resource, label):
    delta = differences(left, right)
    if delta:
        raise UnsafePlan(f"{resource}: unapproved {label}: {', '.join(delta[:12])}")


def container_without_image(text, expected_image, resource):
    containers = json.loads(text)
    if not isinstance(containers, list) or len(containers) != 1:
        raise UnsafePlan(f"{resource}: expected exactly one generator container")
    c = copy.deepcopy(containers[0])
    if c.get("name") != "phase5-generator" or c.get("image") != expected_image:
        raise UnsafePlan(f"{resource}: unexpected generator container/image")
    c.pop("image")
    # AWS provider may reorder environment and secrets between state and config.
    # Names must remain unique; values are still compared exactly.
    for key in ("environment", "secrets"):
        if key in c:
            entries = c[key]
            if not isinstance(entries, list):
                raise UnsafePlan(f"{resource}: invalid {key}")
            names = [e.get("name") for e in entries]
            if any(not n for n in names) or len(names) != len(set(names)):
                raise UnsafePlan(f"{resource}: duplicate/invalid {key} name")
            c[key] = sorted(entries, key=lambda entry: entry["name"])
    return c


def validate_plan(
    plan, *, previous_lambda_image, new_lambda_image,
    previous_generator_image, new_generator_image, generator_task,
):
    changes = {
        c["address"]: c["change"]
        for c in plan.get("resource_changes", [])
        if c["change"].get("actions") != ["no-op"]
    }
    if set(changes) != ALLOWED:
        extra = sorted(set(changes) - ALLOWED)
        missing = sorted(ALLOWED - set(changes))
        raise UnsafePlan(f"Unexpected plan addresses: extra={extra}; missing={missing}")
    for addr in sorted(changes):
        ch = changes[addr]
        actions = ch["actions"]
        old = copy.deepcopy(ch.get("before"))
        new = copy.deepcopy(ch.get("after"))
        if addr in LAMBDA:
            if actions != ["update"]:
                raise UnsafePlan(f"{addr}: Lambda action must be update")
            if old["image_uri"] != previous_lambda_image or new["image_uri"] != new_lambda_image:
                raise UnsafePlan(f"{addr}: unexpected Lambda image change")
            for obj in (old, new):
                obj.pop("image_uri")
                for field in LAMBDA_COMPUTED:
                    obj.pop(field, None)
            if addr.endswith("phase5_controller[0]"):
                env_before = old["environment"][0]["variables"]
                env_after = new["environment"][0]["variables"]
                if env_after.get("PHASE6_GLUE_JOB_NAME") != JOB:
                    raise UnsafePlan(f"{addr}: missing Glue job opt-in")
                if "PHASE6_GLUE_JOB_NAME" in env_before:
                    raise UnsafePlan(f"{addr}: controller already lease-enabled")
                env_after.pop("PHASE6_GLUE_JOB_NAME")
            require_equal(old, new, addr, "Lambda configuration changes")
        elif addr == GENERATOR:
            if actions not in (["delete", "create"], ["create", "delete"]):
                raise UnsafePlan(f"{addr}: expected task-definition replacement")
            old_container = container_without_image(
                old["container_definitions"], previous_generator_image, addr)
            new_container = container_without_image(
                new["container_definitions"], new_generator_image, addr)
            require_equal(old_container, new_container, addr, "container changes")
            for obj in (old, new):
                obj.pop("container_definitions")
                for field in TASK_COMPUTED:
                    obj.pop(field, None)
            require_equal(old, new, addr, "task-definition changes")
        elif addr in SCHEDULES:
            if actions != ["update"]:
                raise UnsafePlan(f"{addr}: expected scheduler update")
            old_target = old["target"][0]
            new_target = new["target"][0]
            old_task = old_target["ecs_parameters"][0]["task_definition_arn"]
            new_task = new_target["ecs_parameters"][0]["task_definition_arn"]
            if old_task != generator_task or (new_task is not None and new_task == generator_task):
                raise UnsafePlan(f"{addr}: unexpected generator task ARN transition")
            # The replacement revision is unknown until Terraform registers it.
            old_target["ecs_parameters"][0].pop("task_definition_arn")
            new_target["ecs_parameters"][0].pop("task_definition_arn")
            require_equal(old, new, addr, "scheduler changes")
        elif addr == POLICY:
            if actions != ["create"] or old is not None:
                raise UnsafePlan(f"{addr}: expected new least-privilege IAM policy only")
            if new.get("name") != "phase6-read-glue-status-for-safe-rds-stop":
                raise UnsafePlan(f"{addr}: wrong IAM policy name")
            statements = json.loads(new["policy"])["Statement"]
            if len(statements) != 1:
                raise UnsafePlan(f"{addr}: policy must have one statement")
            st = statements[0]
            if st.get("Effect") != "Allow" or st.get("Action") != ["glue:GetJobRun"]:
                raise UnsafePlan(f"{addr}: unexpected IAM action")
            if not str(st.get("Resource", "")).endswith(":job/" + JOB):
                raise UnsafePlan(f"{addr}: unexpected Glue IAM resource")
    return sorted(changes)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--plan", required=True)
    p.add_argument("--old-lambda-image", required=True)
    p.add_argument("--new-lambda-image", required=True)
    p.add_argument("--old-generator-image", required=True)
    p.add_argument("--new-generator-image", required=True)
    p.add_argument("--generator-task", required=True)
    a = p.parse_args()
    plan = json.loads(Path(a.plan).read_text(encoding="utf-8"))
    checked = validate_plan(
        plan,
        previous_lambda_image=a.old_lambda_image, new_lambda_image=a.new_lambda_image,
        previous_generator_image=a.old_generator_image,
        new_generator_image=a.new_generator_image, generator_task=a.generator_task,
    )
    print(f"Verified exact seven-resource release plan ({len(checked)} addresses).")
    for address in checked:
        print("  " + address)


if __name__ == "__main__":
    main()
