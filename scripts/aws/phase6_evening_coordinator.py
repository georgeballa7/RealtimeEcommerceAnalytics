"""Low-cost scheduled coordinator: afternoon Shopify -> RDS -> Glue Silver.

Uses existing controller DynamoDB table to coordinate RDS and Glue; no extra
database or queue. Runs every five minutes during the Berlin evening window,
but launches at most one Glue snapshot per day, after writes are quiescent.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError

REGION = "eu-central-1"
TABLE = "ecommerce-demo-phase5-controller"
EVENTS_TABLE = "ecommerce-demo-phase5-events"
CTRL_ID = "phase5-controller"
DAY_ID = "phase6-evening"
CLUSTER = "ecommerce-demo"
RDS_ID = "ecommerce-postgres-dev"
JOB = "ecommerce-phase6-shopify-silver-demo"
LOADER = "ecommerce-phase6-silver-redshift-loader-demo"
QUEUE = "https://sqs.eu-central-1.amazonaws.com/825414820081/ecommerce-demo-events"
BUCKET = "ecommerce-demo-landing-393e7fdc5c01861ae4e5c25ad6"
TTL = 45 * 60
QUIET = 10 * 60
ACTIVE = {"STARTING", "RUNNING", "WAITING", "STOPPING"}
FAILURES = {"FAILED", "ERROR", "TIMEOUT", "STOPPED", "EXPIRED"}


def log(action, **fields):
    print(json.dumps({"phase6_coordinator": action, **fields}, sort_keys=True))
    return {"action": action, **fields}


def quiet_now(now, controller, dynamodb, sqs, ecs):
    if not controller.get("schema_ready") or not controller.get("webhooks_ready"):
        return "phase5_not_ready"
    if controller.get("error_hold") or controller.get("worker_task_arn"):
        return "projection_not_finished"
    last_event = controller.get("last_event_at")
    if not last_event:
        return "no_projected_evening_events"
    event_time = datetime.fromisoformat(last_event)
    cutoff = now.astimezone(ZoneInfo("Europe/Berlin")).replace(
        hour=16, minute=0, second=0, microsecond=0
    ).astimezone(timezone.utc)
    if event_time < cutoff:
        return "no_after_16_events"
    last_work = controller.get("last_work_at")
    activity = max(event_time, datetime.fromisoformat(last_work)) if last_work else event_time
    if now.timestamp() - activity.timestamp() < QUIET:
        return "ingestion_quiet_window"
    # Pending projected events live in the Phase 5 event index, not the controller table.
    events = dynamodb.Table(EVENTS_TABLE)
    pending = events.query(
        IndexName="by_state",
        KeyConditionExpression="#state=:pending",
        ExpressionAttributeNames={"#state": "state"},
        ExpressionAttributeValues={":pending": "pending"},
        Limit=1,
    )
    if pending.get("Items"):
        return "pending_projection_events"
    attrs = sqs.get_queue_attributes(
        QueueUrl=QUEUE,
        AttributeNames=["ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    if any(int(attrs.get(name, "0")) for name in (
        "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"
    )):
        return "sqs_not_empty"
    for desired in ("RUNNING", "PENDING"):
        if ecs.list_tasks(cluster=CLUSTER, desiredStatus=desired).get("taskArns"):
            return "ecs_workload_still_active"
    return None


def reserve(ddb, day, batch, owner, now):
    # Atomically own both the controller Glue stop-blocker and daily launch
    # record; no partial reservation if controller is stopping RDS.
    ddb.transact_write_items(
        TransactItems=[
            {"Update": {
                "TableName": TABLE, "Key": {"id": {"S": CTRL_ID}},
                "UpdateExpression": "SET glue_lease_owner=:owner, glue_lease_expires_at=:exp",
                "ConditionExpression": (
                    "(attribute_not_exists(lease_until) OR lease_until < :now) "
                    "AND attribute_not_exists(glue_lease_owner)"
                ),
                "ExpressionAttributeValues": {
                    ":owner": {"S": owner}, ":exp": {"N": str(now + TTL)},
                    ":now": {"N": str(now)},
                },
            }},
            {"Put": {
                "TableName": TABLE,
                "Item": {
                    "id": {"S": DAY_ID}, "day": {"S": day},
                    "phase": {"S": "reserved"}, "owner": {"S": owner},
                    "batch_id": {"S": batch},
                },
                "ConditionExpression": "attribute_not_exists(#day) OR #day <> :day",
                "ExpressionAttributeNames": {"#day": "day"},
                "ExpressionAttributeValues": {":day": {"S": day}},
            }},
        ]
    )


def renew(control, owner, now):
    control.update_item(
        Key={"id": CTRL_ID},
        UpdateExpression="SET glue_lease_expires_at=:exp",
        ConditionExpression="glue_lease_owner=:owner AND glue_lease_expires_at > :now",
        ExpressionAttributeValues={
            ":owner": owner, ":exp": now + TTL, ":now": now,
        },
    )


def attach(control, owner, run_id, now):
    control.update_item(
        Key={"id": CTRL_ID},
        UpdateExpression="SET glue_lease_job_run_id=:run, glue_lease_expires_at=:exp",
        ConditionExpression=(
            "glue_lease_owner=:owner AND glue_lease_expires_at > :now "
            "AND attribute_not_exists(glue_lease_job_run_id)"
        ),
        ExpressionAttributeValues={
            ":owner": owner, ":run": run_id,
            ":exp": now + TTL, ":now": now,
        },
    )


def release(control, owner):
    control.update_item(
        Key={"id": CTRL_ID},
        UpdateExpression="REMOVE glue_lease_owner, glue_lease_expires_at, glue_lease_job_run_id",
        ConditionExpression="glue_lease_owner=:owner",
        ExpressionAttributeValues={":owner": owner},
    )


def handler(event, context):
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo("Europe/Berlin"))
    if not (16 <= local.hour <= 21):
        return log("outside_evening_window")
    day = local.date().isoformat()
    aws = boto3.Session(region_name=REGION)
    ddb = aws.resource("dynamodb")
    table = ddb.Table(TABLE)
    previous = table.get_item(Key={"id": DAY_ID}, ConsistentRead=True).get("Item", {})
    ctrl = table.get_item(Key={"id": CTRL_ID}, ConsistentRead=True).get("Item", {})
    glue = aws.client("glue")
    rds = aws.client("rds")
    if previous.get("day") != day:
        reason = quiet_now(now, ctrl, ddb, aws.client("sqs"), aws.client("ecs"))
        if reason:
            return log("waiting", reason=reason)
        owner = str(uuid4())
        batch = now.strftime("%Y%m%dT%H%M%SZ")
        try:
            reserve(aws.client("dynamodb"), day, batch, owner, int(now.timestamp()))
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in (
                "TransactionCanceledException", "ConditionalCheckFailedException"
            ):
                return log("reservation_busy")
            raise
        previous = {"day": day, "phase": "reserved", "owner": owner, "batch_id": batch}
    phase = previous["phase"]
    if phase in ("dispatched", "failed"):
        return log(phase, batch=previous["batch_id"])
    owner = previous["owner"]
    renew(table, owner, int(now.timestamp()))
    if phase == "reserved":
        status = rds.describe_db_instances(DBInstanceIdentifier=RDS_ID)["DBInstances"][0]["DBInstanceStatus"]
        if status == "stopping":
            return log("rds_stopping", batch=previous["batch_id"])
        if status == "stopped":
            rds.start_db_instance(DBInstanceIdentifier=RDS_ID)
            return log("rds_start_requested", batch=previous["batch_id"])
        if status != "available":
            return log("waiting_for_rds", status=status)
        batch = previous["batch_id"]
        # Recover from a crash after starting Glue but before recording the run.
        matches = [
            x for x in glue.get_job_runs(JobName=JOB, MaxResults=10).get("JobRuns", [])
            if x.get("Arguments", {}).get("--batch-id") == batch
        ]
        if matches:
            run_id = matches[0]["Id"]
        else:
            run_id = glue.start_job_run(JobName=JOB, Arguments={"--batch-id": batch})["JobRunId"]
        attach(table, owner, run_id, int(now.timestamp()))
        table.update_item(
            Key={"id": DAY_ID},
            UpdateExpression="SET phase=:phase, run_id=:run",
            ConditionExpression="day=:day AND #owner=:owner",
            ExpressionAttributeNames={"#owner": "owner"},
            ExpressionAttributeValues={
                ":phase": "started", ":run": run_id, ":day": day, ":owner": owner,
            },
        )
        return log("glue_started", batch=batch, run_id=run_id)
    if phase == "started":
        run_id = previous["run_id"]
        state = glue.get_job_run(JobName=JOB, RunId=run_id)["JobRun"]["JobRunState"]
        if state in ACTIVE:
            return log("glue_running", run_id=run_id, state=state)
        if state not in FAILURES | {"SUCCEEDED"}:
            raise RuntimeError("Unexpected Glue state: " + state)
        if state == "SUCCEEDED":
            key = f"warehouse/shopify_snapshots/v1/{previous['batch_id']}/silver_manifest.json"
            aws.client("s3").head_object(Bucket=BUCKET, Key=key)
            dispatched = aws.client("lambda").invoke(
                FunctionName=LOADER, InvocationType="Event",
                Payload=json.dumps({"source": "phase6-evening", "run_id": run_id}).encode(),
            )
            if dispatched.get("StatusCode") != 202:
                raise RuntimeError("Silver loader invocation was not accepted")
            next_phase = "dispatched"
        else:
            next_phase = "failed"
        # Only release the controller stop blocker after Glue is terminal.
        release(table, owner)
        table.update_item(
            Key={"id": DAY_ID},
            UpdateExpression="SET phase=:phase, glue_status=:status",
            ConditionExpression="day=:day AND #owner=:owner",
            ExpressionAttributeNames={"#owner": "owner"},
            ExpressionAttributeValues={
                ":phase": next_phase, ":status": state, ":day": day, ":owner": owner,
            },
        )
        return log(next_phase, run_id=run_id, glue_status=state)
    raise RuntimeError("Unknown Phase6 coordinator phase: " + str(phase))
