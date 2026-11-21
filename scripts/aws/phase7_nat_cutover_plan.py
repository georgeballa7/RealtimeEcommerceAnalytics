"""Read-only Phase 7 on-demand NAT cutover preflight and Terraform plan gate.

Never calls AWS, creates a NAT gateway, or applies Terraform. The caller supplies
local 'terraform show -json' files. Sensitive state/plan files are temporary.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


class UnsafePlan(ValueError):
    pass


STATIC_NAT = "aws_nat_gateway.ecommerce[0]"
STATIC_ROUTES = {f"aws_route.application[{i}]" for i in range(2)}
NAT_CREATE = {
    "aws_cloudwatch_log_group.phase6_nat_lifecycle[0]",
    "aws_iam_role.phase6_nat_lifecycle[0]",
    "aws_iam_role_policy_attachment.phase6_nat_logs[0]",
    "aws_iam_role_policy.phase6_nat_lifecycle[0]",
    "aws_lambda_function.phase6_nat_lifecycle[0]",
    "aws_iam_role.phase6_nat_schedule[0]",
    "aws_iam_role_policy.phase6_nat_schedule[0]",
    "aws_scheduler_schedule.phase6_nat_lifecycle[0]",
}
EXPECTED_DELETES = {STATIC_NAT, *STATIC_ROUTES}
EXPECTED_CREATES = NAT_CREATE

TASK_IMAGES = {
    "aws_ecs_task_definition.api[0]": "api_image",
    "aws_ecs_task_definition.consumer[0]": "consumer_image",
    "aws_ecs_task_definition.spark[0]": "spark_image",
    "aws_ecs_task_definition.customer_sync[0]": "customer_sync_image",
    "aws_ecs_task_definition.db_bootstrap[0]": "db_bootstrap_image",
    "aws_ecs_task_definition.catalog_sync[0]": "catalog_sync_image",
    "aws_ecs_task_definition.phase5_projector[0]": "phase5_projector_image",
    "aws_ecs_task_definition.phase5_generator[0]": "phase5_generator_image",
    "aws_ecs_task_definition.phase5_lifecycle[0]": "phase5_lifecycle_image",
    "aws_ecs_task_definition.phase5_migration[0]": "phase5_migration_image",
    "aws_ecs_task_definition.phase5_pricing[0]": "phase5_pricing_image",
    "aws_ecs_task_definition.phase6_rds_exporter[0]": "phase6_rds_exporter_image",
    "aws_ecs_task_definition.phase6_dbt[0]": "phase6_dbt_image",
}


def index_resources(state: dict) -> dict:
    module = state.get("values", {}).get("root_module")
    if not isinstance(module, dict):
        raise UnsafePlan("No Terraform state values/root_module; load the correct initialized backend")
    found = {}

    def visit(node):
        for resource in node.get("resources", []):
            addr = resource["address"]
            if addr in found:
                raise UnsafePlan("Duplicate state address: " + addr)
            found[addr] = resource.get("values", {})
        for child in node.get("child_modules", []):
            visit(child)

    visit(module)
    return found


def image_from_task(values: dict) -> str:
    definition = values.get("container_definitions")
    if not isinstance(definition, str):
        raise UnsafePlan("Task container_definitions missing from state")
    try:
        containers = json.loads(definition)
    except json.JSONDecodeError as exc:
        raise UnsafePlan("Invalid ECS task container definitions in state") from exc
    if len(containers) != 1 or not containers[0].get("image"):
        raise UnsafePlan("Expected exactly one container/image in task definition")
    uri = containers[0]["image"]
    if not isinstance(uri, str) or not re.search(r"@sha256:[a-f0-9]{64}$", uri):
        raise UnsafePlan("Existing ECS task image is not immutable-digest pinned")
    return uri


def snapshot_vars(state: dict, account: str) -> dict:
    """Reconstruct deployed opt-in toggles and images rather than use defaults.

    Any missing/incorrect inputs show up as unrelated changes and are rejected
    by review_plan, never silently applied.
    """
    items = index_resources(state)
    required = {STATIC_NAT, *STATIC_ROUTES}
    if not required.issubset(items):
        raise UnsafePlan("Expected a single deployed static NAT and both Terraform-owned default routes")
    nats = [x for x in items if x.startswith("aws_nat_gateway.ecommerce[")]
    eips = [x for x in items if x.startswith("aws_eip.nat[")]
    if nats != [STATIC_NAT] or eips != ["aws_eip.nat[0]"]:
        raise UnsafePlan("Expected exactly one Terraform-managed NAT and reserved EIP")
    nat_id = items[STATIC_NAT].get("id")
    if not nat_id or any(items[x].get("nat_gateway_id") != nat_id for x in STATIC_ROUTES):
        raise UnsafePlan("Static NAT route identifiers differ from state")
    if "aws_vpc_endpoint.s3" not in items:
        raise UnsafePlan("Existing S3 Gateway endpoint missing from state")
    cluster = items.get("aws_ecs_cluster.ecommerce", {}).get("name")
    if cluster != "ecommerce-demo":
        raise UnsafePlan("Unexpected ECS cluster / environment: " + str(cluster))

    result = {
        "aws_account_id": account,
        "aws_profile": "ecommerce-admin",
        "aws_region": "eu-central-1",
        "nat_gateway_count": 1,
        "nat_gateway_mode": "on_demand",
        "enable_redshift_warehouse":
            "aws_redshiftserverless_workgroup.analytics[0]" in items,
        "enable_phase6_glue_silver":
            "aws_glue_job.phase6_shopify_silver[0]" in items,
        "enable_phase6_glue_rds_lease":
            "aws_iam_role_policy.phase6_controller_glue_inspect[0]" in items,
        "enable_phase6_dbt_runner":
            "aws_ecr_repository.phase6_dbt[0]" in items,
        "enable_phase5_legacy_lifecycle_schedule":
            "aws_scheduler_schedule.phase5_lifecycle[0]" in items,
    }
    for address, variable in TASK_IMAGES.items():
        if address in items:
            result[variable] = image_from_task(items[address])

    # Preserve all three independently deployed Phase 5 Lambda image pointers;
    # a mismatch is operational drift, not something a NAT plan may "repair".
    lambdas = [
        "aws_lambda_function.phase5_fastapi[0]",
        "aws_lambda_function.phase5_landing[0]",
        "aws_lambda_function.phase5_controller[0]",
    ]
    missing = [x for x in lambdas if x not in items]
    if missing:
        raise UnsafePlan("Missing deployed Phase 5 Lambda from state: " + str(missing))
    images = {items[x].get("image_uri") for x in lambdas}
    if len(images) != 1 or not all(isinstance(uri, str) and re.search(
            r"@sha256:[a-f0-9]{64}$", uri) for uri in images):
        raise UnsafePlan("Phase 5 Lambda images must be identical and digest pinned")
    result["phase5_lambda_image"] = images.pop()
    api_env = items[lambdas[0]].get("environment") or []
    if not isinstance(api_env, list) or len(api_env) != 1:
        raise UnsafePlan("Missing Phase 5 API Lambda environment")
    env = api_env[0].get("variables") or {}
    for env_key, variable in [
        ("SHOPIFY_SHOP_DOMAIN", "shopify_shop_domain"),
        ("SHOPIFY_WEBHOOK_SECRET_ARN", "shopify_webhook_secret_arn"),
    ]:
        if not env.get(env_key):
            raise UnsafePlan("Missing Phase 5 environment variable: " + env_key)
        result[variable] = env[env_key]

    # Generator gets the Shopify client ARN from its existing ECS secret ref.
    generator = items.get("aws_ecs_task_definition.phase5_generator[0]")
    if not generator:
        raise UnsafePlan("Phase 5 generator task missing from state")
    defs = json.loads(generator["container_definitions"])
    secret_list = defs[0].get("secrets", [])
    refs = [e.get("valueFrom", "") for e in secret_list if
            e.get("name") == "SHOPIFY_CLIENT_ID"]
    if len(refs) != 1 or not refs[0].endswith(":client_id::"):
        raise UnsafePlan("Shopify client secret ARN not found in generator task")
    result["shopify_client_credentials_secret_arn"] = refs[0][:-len(":client_id::")]

    # Optional older API/ALB stack can only stay enabled if its certificate is
    # preserved; otherwise unrelated ALB deletions would be proposed.
    if "api_image" in result:
        listener = items.get("aws_lb_listener.https[0]")
        if not listener or not listener.get("certificate_arn"):
            raise UnsafePlan("Existing ECS API/ALB certificate missing from state")
        result["acm_certificate_arn"] = listener["certificate_arn"]

    for prefix, variable in (
        ("aws_subnet.public", "public_subnet_cidrs"),
        ("aws_subnet.application", "application_subnet_cidrs"),
        ("aws_subnet.database", "private_subnet_cidrs"),
    ):
        entries = [f"{prefix}[0]", f"{prefix}[1]"]
        if not all(x in items for x in entries):
            raise UnsafePlan("Expected both Terraform-managed subnets: " + prefix)
        result[variable] = [items[x]["cidr_block"] for x in entries]

    database = items.get("aws_db_instance.ecommerce", {})
    if not database:
        raise UnsafePlan("Deployed RDS database missing from state")
    for state_key, variable in (
        ("instance_class", "db_instance_class"),
        ("allocated_storage", "db_allocated_storage"),
        ("multi_az", "db_multi_az"),
        ("db_name", "db_name"),
        ("username", "db_master_username"),
    ):
        if database.get(state_key) is not None:
            result[variable] = database[state_key]

    if "aws_vpc.ecommerce" in items:
        result["vpc_cidr"] = items["aws_vpc.ecommerce"]["cidr_block"]
    sqs = items.get("aws_sqs_queue.events", {})
    if sqs.get("visibility_timeout_seconds") is not None:
        result["sqs_visibility_timeout_seconds"] = sqs["visibility_timeout_seconds"]
    for key, name in (
        ("aws_redshiftserverless_usage_limit.analytics_daily[0]", "redshift_daily_rpu_hour_limit"),
        ("aws_redshiftserverless_usage_limit.analytics_monthly[0]", "redshift_monthly_rpu_hour_limit"),
    ):
        if key in items:
            result[name] = items[key]["amount"]
    return result


def review_plan(plan: dict) -> dict:
    if not isinstance(plan.get("resource_changes"), list):
        raise UnsafePlan("Terraform JSON plan has no resource changes")
    seen = {}
    for resource in plan["resource_changes"]:
        address = resource["address"]
        actions = resource["change"]["actions"]
        if actions == ["no-op"]:
            continue
        if resource.get("mode") == "data" and actions == ["read"]:
            if address != "data.archive_file.phase6_nat_lifecycle[0]":
                raise UnsafePlan("Unexpected Terraform data-source read: " + address)
            continue
        if address in seen:
            raise UnsafePlan("Duplicate changed Terraform resource: " + address)
        seen[address] = actions
    bad = {
        addr: acts for addr, acts in seen.items()
        if not ((addr in EXPECTED_DELETES and acts == ["delete"])
                or (addr in EXPECTED_CREATES and acts == ["create"]))
    }
    if bad:
        raise UnsafePlan("Unrelated or unsafe Terraform changes (REFUSE APPLY): " +
                         json.dumps(bad, sort_keys=True))
    if set(seen) != EXPECTED_DELETES | EXPECTED_CREATES:
        missing = (EXPECTED_DELETES | EXPECTED_CREATES) - set(seen)
        raise UnsafePlan("Incomplete/unexpected NAT cutover plan: missing " +
                         ", ".join(sorted(missing)))
    return {
        "decision": "PLAN_ONLY_REVIEW_REQUIRED",
        "nat_gateway_deletes": 1,
        "application_default_route_deletes": 2,
        "new_nat_lifecycle_resources": len(EXPECTED_CREATES),
        "other_changes": 0,
        "apply_permitted_by_this_tool": False,
        "warning": (
            "A Terraform apply would remove current NAT/routes before the new "
            "scheduled Lambda creates its first on-demand NAT. Schedule a quiet "
            "window and prepare an independently reviewed rollback."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    snapshot = sub.add_parser("snapshot")
    snapshot.add_argument("--state", required=True, type=Path)
    snapshot.add_argument("--output", required=True, type=Path)
    snapshot.add_argument("--account", required=True)
    check = sub.add_parser("review")
    check.add_argument("--plan", required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.command == "snapshot":
            if not re.fullmatch(r"[0-9]{12}", args.account):
                raise UnsafePlan("Invalid AWS account ID")
            state = json.loads(args.state.read_text(encoding="utf-8-sig"))
            variables = snapshot_vars(state, args.account)
            args.output.write_text(json.dumps(variables, indent=2) + "\n", encoding="utf-8")
            print("Verified static NAT + deployed Terraform options. Wrote temporary plan inputs.")
        else:
            plan = json.loads(args.plan.read_text(encoding="utf-8-sig"))
            print(json.dumps(review_plan(plan), indent=2))
    except (UnsafePlan, KeyError, TypeError, ValueError) as exc:
        parser.exit(2, "CUTOVER PLAN REJECTED: " + str(exc) + "\n")


if __name__ == "__main__":
    main()
