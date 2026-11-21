"""Event-driven, completion-gated dbt ECS runner. Fail closed by default.

Signals: successful Redshift-loader Lambda invocation and ECS task STOPPED.
Recovery: 5-minute reconciliation. Source of truth: verified warehouse slots
in the existing Phase 5 DynamoDB controller table, never clock time alone.
"""
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError

REGION = "eu-central-1"
ACCOUNT = "825414820081"
TABLE = "ecommerce-demo-phase5-controller"
CLUSTER = "ecommerce-demo"
FAMILY = "ecommerce-phase6-dbt-demo"
SUBNETS = ["subnet-0248e35f98ea3fbf6", "subnet-0481cb476d153d5ba"]
SECURITY_GROUP = "sg-0d06aade34183743b"
BAD_DIGEST = "sha256:40cd27823d7be5fa2a6fd7f38601781cfd11a90a8d15e237e794a6baa4bbab25"
BERLIN = ZoneInfo("Europe/Berlin")


def log(state, **kwargs):
    record = {"phase7_dbt": state, **kwargs}
    print(json.dumps(record, sort_keys=True))
    return record


def ready_slots(table):
    local = datetime.now(timezone.utc).astimezone(BERLIN)
    day = local.date().isoformat()
    candidates = [("morning", day, "phase6-morning"),
                  ("evening", day, "phase6-evening")]
    if local.hour < 3:
        candidates.append(("evening", (local.date() - timedelta(days=1)).isoformat(),
                           "phase6-evening"))
    verified = []
    for slot, date, key in candidates:
        item = table.get_item(Key={"id": key}, ConsistentRead=True).get("Item", {})
        if item.get("day") != date or item.get("phase") != "completed":
            continue
        batch = item.get("batch_id", "")
        if (not item.get("warehouse_verified_at") or
                not re.fullmatch(r"[0-9]{8}T[0-9]{6}Z", batch) or
                not str(item.get("run_id", "")).startswith("jr_")):
            return None
        verified.append((slot, date, batch))
    return sorted(set(verified), key=lambda v: v[2])


def route_ready(ec2):
    response = ec2.describe_route_tables(
        Filters=[{"Name": "association.subnet-id", "Values": SUBNETS}])
    rows = response.get("RouteTables", [])
    if len({r["RouteTableId"] for r in rows}) != 2:
        return False
    nat_ids = []
    for row in rows:
        routes = [r for r in row.get("Routes", [])
                  if r.get("DestinationCidrBlock") == "0.0.0.0/0"
                  and r.get("State") == "active" and r.get("NatGatewayId")]
        if len(routes) != 1:
            return False
        nat_ids.append(routes[0]["NatGatewayId"])
    if len(set(nat_ids)) != 1:
        return False
    nats = ec2.describe_nat_gateways(NatGatewayIds=[nat_ids[0]]).get("NatGateways", [])
    return len(nats) == 1 and nats[0].get("State") == "available"


def active_dbt_tasks(ecs):
    for status in ("RUNNING", "PENDING"):
        if ecs.list_tasks(cluster=CLUSTER, family=FAMILY,
                          desiredStatus=status).get("taskArns"):
            return True
    return False


def reconcile(table, ecs, slot, day, batch):
    key = f"phase7-dbt:{slot}:{batch}"
    item = table.get_item(Key={"id": key}, ConsistentRead=True).get("Item", {})
    state = item.get("state")
    if state in ("succeeded", "failed"):
        return log("terminal_" + state, key=key)
    if state in ("running", "launching"):
        arn = item.get("task_arn")
        if not arn:
            return log("launch_unconfirmed_manual_review", key=key)
        matches = ecs.describe_tasks(cluster=CLUSTER, tasks=[arn]).get("tasks", [])
        if len(matches) != 1 or matches[0].get("lastStatus") != "STOPPED":
            return log("task_still_active_or_pending", key=key)
        containers = [c for c in matches[0].get("containers", [])
                      if c.get("name") == "phase6-dbt"]
        outcome = ("succeeded" if len(containers) == 1
                   and containers[0].get("exitCode") == 0 else "failed")
        table.update_item(Key={"id": key},
                          UpdateExpression="SET #s=:s, finished_at=:now",
                          ExpressionAttributeNames={"#s": "state"},
                          ExpressionAttributeValues={
                              ":s": outcome, ":now": int(time.time())})
        try:
            table.delete_item(Key={"id": "phase7-dbt-global-lock"},
                              ConditionExpression="#o=:owner",
                              ExpressionAttributeNames={"#o": "owner"},
                              ExpressionAttributeValues={":owner": key})
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
        return log("task_" + outcome, key=key)
    if os.environ.get("DBT_LAUNCH_ENABLED") != "true":
        return log("disabled_until_image_auth_grants_validated", key=key)
    task_arn = os.environ.get("DBT_TASK_DEFINITION", "")
    if not re.fullmatch(
        r"arn:aws:ecs:eu-central-1:825414820081:task-definition/ecommerce-phase6-dbt-demo:[0-9]+",
        task_arn,
    ):
        return log("unsafe_task_definition_config")
    definition = ecs.describe_task_definition(taskDefinition=task_arn)["taskDefinition"]
    container = definition.get("containerDefinitions", [])
    if len(container) != 1 or container[0].get("name") != "phase6-dbt":
        return log("unexpected_task_container")
    uri = container[0].get("image", "")
    if not re.fullmatch(
        r"825414820081.dkr.ecr.eu-central-1.amazonaws.com/ecommerce-phase6-dbt-demo@sha256:[a-f0-9]{64}",
        uri,
    ) or uri.endswith(BAD_DIGEST):
        return log("refused_old_or_mutable_image", key=key)
    if not route_ready(boto3.client("ec2", region_name=REGION)):
        return log("waiting_for_active_nat_on_both_routes", key=key)
    if active_dbt_tasks(ecs):
        return log("another_dbt_task_active", key=key)
    now = int(time.time())
    try:
        table.put_item(
            Item={"id": "phase7-dbt-global-lock", "owner": key, "expires_at": now + 7200},
            ConditionExpression="attribute_not_exists(id) OR expires_at < :now",
            ExpressionAttributeValues={":now": now})
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return log("global_lock_busy", key=key)
        raise
    try:
        table.put_item(
            Item={"id": key, "state": "launching", "slot": slot, "day": day,
                  "batch_id": batch, "requested_at": now},
            ConditionExpression="attribute_not_exists(id)")
        response = ecs.run_task(
            cluster=CLUSTER, taskDefinition=task_arn, launchType="FARGATE",
            startedBy="phase7-dbt-" + batch.lower(), count=1,
            overrides={"containerOverrides": [{
                "name": "phase6-dbt", "command": ["--action", "build", "--select", "*"]}]},
            networkConfiguration={"awsvpcConfiguration": {
                "subnets": SUBNETS, "securityGroups": [SECURITY_GROUP],
                "assignPublicIp": "DISABLED"}})
        if response.get("failures") or len(response.get("tasks", [])) != 1:
            raise RuntimeError("ECS task not accepted")
        arn = response["tasks"][0]["taskArn"]
        table.update_item(
            Key={"id": key},
            UpdateExpression="SET #s=:s, task_arn=:arn",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={":s": "running", ":arn": arn})
        return log("dbt_started", key=key, task_arn=arn)
    except Exception:
        # Leave lock and marker for manual recovery: never blindly duplicate a task.
        log("launch_uncertain_manual_review", key=key)
        raise


def handler(event, context):
    table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE)
    ecs = boto3.client("ecs", region_name=REGION)
    slots = ready_slots(table)
    if slots is None:
        return log("unsafe_unverified_warehouse_slot")
    if not slots:
        return log("waiting_for_warehouse_load_ack")
    return {"results": [reconcile(table, ecs, *slot) for slot in slots]}
