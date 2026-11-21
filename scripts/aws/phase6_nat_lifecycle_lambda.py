"""On-demand public NAT for the private e-commerce application subnet routes.

Terraform owns the EIP, private/public subnet and route table definitions. This
control-plane Lambda owns ONLY tagged, ephemeral NAT gateways and default
routes when Terraform nat_gateway_mode=on_demand. Never changes S3 endpoint
routes, RDS, Shopify schedules, or Terraform state.

Invoked every five minutes via EventBridge Scheduler. Creates NAT in advance
of the 08:00 and 16:00 Berlin generator windows. Releases NAT only after
dependent workloads settle, including the 22:00 lifecycle ECS run.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError

TAG_VALUE = "phase6-dynamic-nat"
BERLIN = ZoneInfo("Europe/Berlin")
GLUE_ACTIVE = {"STARTING", "RUNNING", "STOPPING", "WAITING"}
TERMINAL_PHASES = {"dispatched", "completed", "failed", "warehouse_failed"}
LOCK_ID = "phase6-nat-lifecycle-lock"
LEASE_SECONDS = 90
QUIET_SECONDS = 20 * 60


def slot_window(local: datetime) -> str | None:
    """Prewarm at 07:30 and 15:30; normal run hours use Europe/Berlin DST."""
    minute = local.hour * 60 + local.minute
    if 7 * 60 + 30 <= minute < 14 * 60:
        return "morning"
    if 15 * 60 + 30 <= minute < 23 * 60:
        return "afternoon"
    return None


def can_begin_new_warm(local: datetime, slot: str) -> bool:
    """Do not provision a new gateway for an already-past daily slot.

    An existing open slot may still be recovered later if its NAT disappears.
    """
    minute = local.hour * 60 + local.minute
    if slot == "morning":
        return 7 * 60 + 30 <= minute < 9 * 60
    if slot == "afternoon":
        return 15 * 60 + 30 <= minute < 17 * 60
    return False


def can_consider_release(local: datetime, slot: str) -> bool:
    minute = local.hour * 60 + local.minute
    # The legacy standalone 18:00 task retains NAT only until a verified
    # Terraform cutover disables that schedule. Completion/backlog guards
    # must pass independently before actual NAT deletion.
    if slot == "morning":
        return minute >= 8 * 60 + 30
    if slot == "afternoon":
        # 22:00 lifecycle and its webhooks must finish before NAT release.
        # Past midnight still belongs to the preceding afternoon processing cycle.
        return minute < 3 * 60 or minute >= 22 * 60 + 25
    return True


def is_stale(local: datetime, slot: str) -> bool:
    minute = local.hour * 60 + local.minute
    if slot == "morning":
        return minute >= 14 * 60
    if slot == "afternoon":
        return 3 * 60 <= minute < 7 * 60 + 30
    return True


def log(action: str, **details):
    message = {"nat_lifecycle": action, **details}
    print(json.dumps(message, sort_keys=True))
    return message


def config():
    names = (
        "NAT_ALLOCATION_ID", "NAT_PUBLIC_SUBNET_ID", "NAT_VPC_ID",
        "NAT_APPLICATION_ROUTE_TABLES", "CONTROLLER_TABLE", "EVENTS_TABLE",
        "EVENT_QUEUE_URL", "ECS_CLUSTER", "GLUE_JOB_NAME",
    )
    settings = {name: os.environ.get(name, "") for name in names}
    if any(not v for v in settings.values()):
        raise RuntimeError("Missing NAT lifecycle configuration")
    settings["route_tables"] = settings["NAT_APPLICATION_ROUTE_TABLES"].split(",")
    if len(settings["route_tables"]) != 2 or len(set(settings["route_tables"])) != 2:
        raise RuntimeError("Expected two distinct application route tables")
    if os.environ.get("AWS_REGION", "eu-central-1") != "eu-central-1":
        raise RuntimeError("Wrong NAT lifecycle region")
    return settings


def managed_nat(ec2, vpc_id: str):
    result = ec2.describe_nat_gateways(Filters=[
        {"Name": "vpc-id", "Values": [vpc_id]},
        {"Name": "tag:Purpose", "Values": [TAG_VALUE]},
        {"Name": "state", "Values": ["pending", "available", "deleting", "failed"]},
    ])
    candidates = result.get("NatGateways", [])
    if len(candidates) > 1:
        raise RuntimeError("Multiple on-demand NAT gateways found; stop for manual review")
    return candidates[0] if candidates else None


def verify_existing_route_tables(ec2, route_table_ids):
    data = ec2.describe_route_tables(RouteTableIds=route_table_ids)
    rows = data.get("RouteTables", [])
    if {row["RouteTableId"] for row in rows} != set(route_table_ids):
        raise RuntimeError("Application route table IDs differ from Terraform")
    return rows


def ensure_default_routes(ec2, route_tables, nat_id):
    for table in verify_existing_route_tables(ec2, route_tables):
        existing = next(
            (x for x in table["Routes"] if x.get("DestinationCidrBlock") == "0.0.0.0/0"),
            None,
        )
        if existing:
            if existing.get("NatGatewayId") != nat_id:
                # Never overwrite a Terraform-owned default route or foreign NAT.
                raise RuntimeError("Unexpected application default route; refusing replacement")
            if existing.get("State") != "active":
                raise RuntimeError("NAT default route is present but not active")
        else:
            ec2.create_route(
                RouteTableId=table["RouteTableId"],
                DestinationCidrBlock="0.0.0.0/0",
                NatGatewayId=nat_id,
            )


def remove_default_routes(ec2, route_tables, nat_id):
    for table in verify_existing_route_tables(ec2, route_tables):
        existing = next(
            (x for x in table["Routes"] if x.get("DestinationCidrBlock") == "0.0.0.0/0"),
            None,
        )
        if existing is None:
            continue
        if existing.get("NatGatewayId") != nat_id:
            raise RuntimeError("Refusing to remove an unrecognized default route")
        ec2.delete_route(
            RouteTableId=table["RouteTableId"],
            DestinationCidrBlock="0.0.0.0/0",
        )


def slot_key(day: str, slot: str) -> str:
    return f"phase6-nat:{day}:{slot}"


def load_record(table, day: str, slot: str):
    return table.get_item(Key={"id": slot_key(day, slot)}, ConsistentRead=True).get("Item")


def record_warm(table, day, slot):
    key = slot_key(day, slot)
    try:
        table.put_item(
            Item={"id": key, "day": day, "slot": slot, "phase": "warming"},
            ConditionExpression="attribute_not_exists(id)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
    return table.get_item(Key={"id": key}, ConsistentRead=True)["Item"]


def create_managed_nat(ec2, settings, day, slot):
    allocated = ec2.describe_addresses(AllocationIds=[settings["NAT_ALLOCATION_ID"]])
    addresses = allocated.get("Addresses", [])
    if len(addresses) != 1:
        raise RuntimeError("Missing reserved Elastic IP")
    if addresses[0].get("AssociationId"):
        return log("eip_not_yet_free", slot=slot)
    # ClientToken makes retries idempotent if Lambda fails after CreateNatGateway.
    response = ec2.create_nat_gateway(
        SubnetId=settings["NAT_PUBLIC_SUBNET_ID"],
        AllocationId=settings["NAT_ALLOCATION_ID"],
        ConnectivityType="public",
        ClientToken=f"phase6nat-{day.replace('-', '')}-{slot}",
        TagSpecifications=[{
            "ResourceType": "natgateway",
            "Tags": [
                {"Key": "Project", "Value": "RealtimeEcommerceAnalytics"},
                {"Key": "Purpose", "Value": TAG_VALUE},
                {"Key": "Day", "Value": day},
                {"Key": "Slot", "Value": slot},
            ],
        }],
    )
    nat = response["NatGateway"]
    return log("created", nat_id=nat["NatGatewayId"], slot=slot, state=nat["State"])


def slot_is_stale(local: datetime, day: str, slot: str) -> bool:
    """Evaluate staleness against a slot's own day, across midnight."""
    today = local.date().isoformat()
    if day > today:
        return False
    if day < today:
        return slot == "morning" or local.hour >= 3
    return is_stale(local, slot)


def inactive_workloads(clients, settings, controller, local, slot, *, day=None):
    """Fail closed: only delete NAT when ALL outbound-workload checks pass."""
    window_open = can_consider_release(local, slot)
    # A previous day's morning can be released after midnight, once its own
    # completion and all live workloads have been checked.
    if day and day < local.date().isoformat() and slot == "morning":
        window_open = True
    if not window_open:
        return "scheduled_workload_window"
    if controller.get("worker_task_arn") or controller.get("error_hold"):
        return "projector_not_idle"
    last = [controller.get("last_event_at"), controller.get("last_work_at")]
    timestamps = [datetime.fromisoformat(x) for x in last if x]
    if not timestamps and not slot_is_stale(local, day or local.date().isoformat(), slot):
        return "no_confirmed_activity"
    if timestamps and (datetime.now(timezone.utc) - max(timestamps)).total_seconds() < QUIET_SECONDS:
        return "recent_projected_activity"
    for state in ("RUNNING", "PENDING"):
        if clients["ecs"].list_tasks(
            cluster=settings["ECS_CLUSTER"], desiredStatus=state
        ).get("taskArns"):
            return f"ecs_tasks_{state.lower()}"
    attrs = clients["sqs"].get_queue_attributes(
        QueueUrl=settings["EVENT_QUEUE_URL"],
        AttributeNames=[
            "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible",
        ],
    ).get("Attributes", {})
    if any(int(attrs.get(key, "0")) for key in (
        "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"
    )):
        return "sqs_backlog"
    pending = clients["ddb"].Table(settings["EVENTS_TABLE"]).query(
        IndexName="by_state",
        KeyConditionExpression="#state=:pending",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":pending": "pending"},
        Select="COUNT",
        Limit=1,
    )
    if pending.get("Count", 0):
        return "unprocessed_events"
    runs = clients["glue"].get_job_runs(JobName=settings["GLUE_JOB_NAME"], MaxResults=10)
    if any(x["JobRunState"] in GLUE_ACTIVE for x in runs.get("JobRuns", [])):
        return "glue_running"
    return None


def active_slot_finished(table, day: str, slot: str, local: datetime):
    coordinator_key = "phase6-morning" if slot == "morning" else "phase6-evening"
    status = table.get_item(Key={"id": coordinator_key}, ConsistentRead=True).get(
        "Item", {}
    )
    if status.get("day") == day and status.get("phase") in TERMINAL_PHASES:
        return True
    # Only a stale *idle* pipeline may be shut down after a missed/failed run.
    # Use the original slot date after midnight, not today's unrelated slot.
    # The calling path checks every active workload and backlog independently.
    return slot_is_stale(local, day, slot)


def release_if_idle(ec2, clients, table, settings, nat, local):
    tags = {tag["Key"]: tag["Value"] for tag in nat.get("Tags", [])}
    nat_slot = tags.get("Slot")
    nat_day = tags.get("Day")
    if nat_slot not in ("morning", "afternoon"):
        raise RuntimeError("On-demand NAT has unexpected slot tag")
    try:
        if not nat_day or datetime.fromisoformat(nat_day).date().isoformat() != nat_day:
            raise ValueError("invalid day")
    except ValueError as exc:
        raise RuntimeError("On-demand NAT has unexpected Day tag") from exc
    current_day = local.date().isoformat()
    if nat_day > current_day:
        raise RuntimeError("NAT Day tag is unexpectedly in the future")

    # A single gateway can cover morning + afternoon and may stay alive after
    # midnight. Always evaluate both the creation-day and today's open slots,
    # never infer yesterday's completion state from today's empty records.
    relevant = []
    for day in sorted({nat_day, current_day}):
        for slot in ("morning", "afternoon"):
            item = load_record(table, day, slot)
            if item and item.get("phase") != "closed":
                relevant.append((day, slot))
    if not relevant:
        relevant = [(nat_day, nat_slot)]
    controller = table.get_item(
        Key={"id": "phase5-controller"}, ConsistentRead=True
    ).get("Item", {})
    for day, slot in relevant:
        if not active_slot_finished(table, day, slot, local):
            return log("awaiting_slot_completion", day=day, slot=slot,
                       nat_id=nat["NatGatewayId"])
        # After Redshift warehouse acknowledgement, retain NAT while the
        # corresponding completion-triggered dbt build is pending/running.
        # Even if the event notification is late, the DynamoDB marker is
        # authoritative. A stale idle slot may release NAT to cap costs;
        # active ECS tasks are independently checked immediately below.
        coordinator_id = "phase6-morning" if slot == "morning" else "phase6-evening"
        warehouse = table.get_item(
            Key={"id": coordinator_id}, ConsistentRead=True
        ).get("Item", {})
        if (warehouse.get("day") == day
                and warehouse.get("phase") == "completed"
                and warehouse.get("warehouse_verified_at")):
            dbt_slot = "morning" if slot == "morning" else "evening"
            batch = warehouse.get("batch_id", "")
            dbt = table.get_item(
                Key={"id": f"phase7-dbt:{dbt_slot}:{batch}"}, ConsistentRead=True
            ).get("Item", {})
            if dbt.get("state") != "succeeded" and not slot_is_stale(local, day, slot):
                return log("awaiting_verified_dbt_build", day=day, slot=slot,
                           dbt_state=dbt.get("state", "not_started"),
                           nat_id=nat["NatGatewayId"])
        reason = inactive_workloads(clients, settings, controller, local, slot,
                                    day=day)
        if reason:
            return log("retained", reason=reason, day=day, slot=slot,
                       nat_id=nat["NatGatewayId"])
    nat_id = nat["NatGatewayId"]
    remove_default_routes(ec2, settings["route_tables"], nat_id)
    ec2.delete_nat_gateway(NatGatewayId=nat_id)
    for day, slot in relevant:
        table.update_item(
            Key={"id": slot_key(day, slot)},
            UpdateExpression="SET #phase=:closed",
            ExpressionAttributeNames={"#phase": "phase"},
            ExpressionAttributeValues={":closed": "closed"},
        )
    return log("delete_requested", nat_id=nat_id, slots=relevant)


def acquire_lock(table, now: int, owner: str):
    try:
        table.update_item(
            Key={"id": LOCK_ID},
            UpdateExpression="SET lock_owner=:owner, lock_expires=:expiry",
            ConditionExpression="attribute_not_exists(lock_expires) OR lock_expires < :now",
            ExpressionAttributeValues={
                ":owner": owner, ":expiry": now + LEASE_SECONDS, ":now": now,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def handler(event, context):
    event = event if isinstance(event, dict) else {}
    # A staged controller must never create/delete NAT or routes when static NAT is live.
    if os.environ.get("NAT_LIFECYCLE_ACTIVE", "false").lower() != "true":
        return log("staged_disabled")
    settings = config()
    now = datetime.now(timezone.utc)
    local = now.astimezone(BERLIN)
    today = local.date().isoformat()
    slot = slot_window(local)
    session = boto3.Session(region_name="eu-central-1")
    ddb = session.resource("dynamodb")
    table = ddb.Table(settings["CONTROLLER_TABLE"])
    owner = str(uuid4())
    if not acquire_lock(table, int(now.timestamp()), owner):
        return log("concurrent_check_deferred")
    try:
        ec2 = session.client("ec2")
        clients = {
            "ddb": ddb,
            "ecs": session.client("ecs"),
            "sqs": session.client("sqs"),
            "glue": session.client("glue"),
        }
        nat = managed_nat(ec2, settings["NAT_VPC_ID"])
        if nat and nat["State"] in ("deleting", "failed"):
            return log("nat_transition", nat_id=nat["NatGatewayId"], state=nat["State"])
        if nat and nat["State"] == "pending":
            return log("awaiting_nat", nat_id=nat["NatGatewayId"])
        if nat and nat["State"] == "available":
            # A morning NAT may still be available when afternoon prewarming
            # begins. Claim the afternoon record BEFORE considering deletion.
            # Otherwise the controller can tear down the gateway at 15:30.
            if slot and can_begin_new_warm(local, slot):
                record_warm(table, today, slot)
            ensure_default_routes(ec2, settings["route_tables"], nat["NatGatewayId"])
            return release_if_idle(ec2, clients, table, settings, nat, local)
        if slot is None:
            return log("idle_no_nat")
        existing = load_record(table, today, slot)
        if existing and existing.get("phase") == "closed":
            return log("completed_no_nat", slot=slot)
        if not existing:
            if not can_begin_new_warm(local, slot):
                return log("missed_prewarm_window", slot=slot)
            record_warm(table, today, slot)
        return create_managed_nat(ec2, settings, today, slot)
    finally:
        try:
            table.delete_item(
                Key={"id": LOCK_ID},
                ConditionExpression="lock_owner=:owner",
                ExpressionAttributeValues={":owner": owner},
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
