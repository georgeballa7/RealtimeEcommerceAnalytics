"""Low-cost coordinator: morning AND afternoon Shopify -> RDS -> Glue Silver.

Existing 08:00/16:00 Shopify generator schedules create the orders. A separate
five-minute readiness poll checks ingestion/projector quietness and starts at
most ONE Glue snapshot for EACH daily slot. The successful-Glue event triggers
the existing idempotent Silver-to-Redshift loader. No extra generator runs.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

import boto3
from botocore.exceptions import ClientError

REGION = "eu-central-1"
TABLE = "ecommerce-demo-phase5-controller"
EVENTS_TABLE = "ecommerce-demo-phase5-events"
CTRL_ID = "phase5-controller"
# Keep the original afternoon key to preserve existing same-day run records.
# The morning slot gets a distinct idempotency key in the SAME controller table.
SLOT_IDS = {"morning": "phase6-morning", "afternoon": "phase6-evening"}
SLOT_HOURS = {"morning": range(8, 16), "afternoon": (17, 18, 19, 20, 21, 22, 23, 0, 1, 2)}
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


def quiet_now(now, controller, dynamodb, sqs, ecs, *, slot="afternoon", s3=None):
    if not controller.get("schema_ready") or not controller.get("webhooks_ready"):
        return "phase5_not_ready"
    if controller.get("error_hold") or controller.get("worker_task_arn"):
        return "projection_not_finished"
    # The end-of-day lifecycle publishes this marker only after both Shopify
    # generation slots and settled cancellation/refund processing succeeded.
    # Without it, quiet queues could represent a failed generator, not success.
    if s3 is not None:
        from botocore.exceptions import ClientError
        local = now.astimezone(ZoneInfo("Europe/Berlin"))
        pipeline_day = processing_day(local, slot)
        compact_day = pipeline_day.replace("-", "")
        required = [("generation", slot)]
        if slot == "afternoon":
            required.extend([("generation", "morning"), ("lifecycle", "night")])
        for kind, marker_slot in required:
            if kind == "generation":
                run_id = f"live-{compact_day}-{marker_slot}"
                key = f"simulation/phase5/slots/v1/{run_id}.json"
                expected = {"schema_version": 1, "run_id": run_id,
                            "slot": marker_slot, "generation": "complete"}
            else:
                key = f"simulation/phase5/slots/v1/lifecycle-{pipeline_day}.json"
                expected = {"schema_version": 1, "day": pipeline_day,
                            "lifecycle": "settled",
                            "generation_slots": ["morning", "afternoon"]}
            try:
                marker = json.loads(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read())
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
                    return f"{kind}_{marker_slot}_not_complete"
                raise
            if marker != expected:
                raise RuntimeError(f"Invalid or conflicting readiness marker: {key}")
    last_event = controller.get("last_event_at")
    if not last_event:
        return "no_projected_slot_events"
    event_time = datetime.fromisoformat(last_event)
    if slot not in SLOT_IDS:
        raise ValueError("Unknown pipeline slot")
    start_hour = 8 if slot == "morning" else 16
    local_day = processing_day(now.astimezone(ZoneInfo("Europe/Berlin")), slot)
    cutoff = datetime.fromisoformat(local_day).replace(
        hour=start_hour, minute=0, second=0, microsecond=0,
        tzinfo=ZoneInfo("Europe/Berlin")).astimezone(timezone.utc)
    if event_time < cutoff:
        return "no_events_after_slot_start"
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


def reserve(ddb, day, batch, owner, now, *, day_id):
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
                    "id": {"S": day_id}, "day": {"S": day},
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


def processing_day(local, slot):
    """After midnight the recovering 17:00 lifecycle belongs to yesterday."""
    day = local.date()
    if slot == "afternoon" and local.hour < 3:
        day -= timedelta(days=1)
    return day.isoformat()


def slot_for_time(local):
    """Which generator slot can advance in the current Berlin local time window?"""
    return next((name for name, hours in SLOT_HOURS.items() if local.hour in hours), None)


def handler(event, context):
    now = datetime.now(timezone.utc)
    local = now.astimezone(ZoneInfo("Europe/Berlin"))
    slot = slot_for_time(local)
    if slot is None:
        return log("outside_slot_readiness_windows")
    day_id = SLOT_IDS[slot]
    day = processing_day(local, slot)
    aws = boto3.Session(region_name=REGION)
    ddb = aws.resource("dynamodb")
    table = ddb.Table(TABLE)
    # A morning Glue run still active at 17:00 must complete before the
    # end-of-day lifecycle snapshot reserves its own RDS/Glue lease.
    # Prevent overlapping Glue full-state extracts.
    if slot == "afternoon":
        morning = table.get_item(
            Key={"id": SLOT_IDS["morning"]}, ConsistentRead=True
        ).get("Item", {})
        if morning.get("day") == day and morning.get("phase") in ("reserved", "started"):
            slot = "morning"
            day_id = SLOT_IDS["morning"]
    previous = table.get_item(Key={"id": day_id}, ConsistentRead=True).get("Item", {})
    ctrl = table.get_item(Key={"id": CTRL_ID}, ConsistentRead=True).get("Item", {})
    glue = aws.client("glue")
    rds = aws.client("rds")
    if previous.get("day") != day:
        reason = quiet_now(now, ctrl, ddb, aws.client("sqs"), aws.client("ecs"),
                           slot=slot, s3=aws.client("s3"))
        if reason:
            return log("waiting", slot=slot, reason=reason)
        owner = str(uuid4())
        batch = now.strftime("%Y%m%dT%H%M%SZ")
        try:
            reserve(aws.client("dynamodb"), day, batch, owner, int(now.timestamp()), day_id=day_id)
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in (
                "TransactionCanceledException", "ConditionalCheckFailedException"
            ):
                return log("reservation_busy", slot=slot)
            raise
        previous = {"day": day, "phase": "reserved", "owner": owner, "batch_id": batch}
    phase = previous["phase"]
    if phase in ("completed", "failed", "warehouse_failed"):
        return log(phase, slot=slot, batch=previous["batch_id"])
    if phase == "dispatched":
        attempts = int(previous.get("loader_attempts", 1))
        last = int(previous.get("loader_last_attempt_at", 0))
        if int(now.timestamp()) - last < 20 * 60:
            return log("warehouse_pending", slot=slot, attempts=attempts)
        if attempts >= 3:
            table.update_item(
                Key={"id": day_id},
                UpdateExpression="SET #phase=:failed",
                ConditionExpression="#phase=:dispatched",
                ExpressionAttributeNames={"#phase": "phase"},
                ExpressionAttributeValues={
                    ":failed": "warehouse_failed", ":dispatched": "dispatched"},
            )
            return log("warehouse_failed", slot=slot, reason="loader_ack_timeout")
        table.update_item(
            Key={"id": day_id},
            UpdateExpression="SET loader_attempts=:attempts, loader_last_attempt_at=:now",
            ConditionExpression="#phase=:dispatched",
            ExpressionAttributeNames={"#phase": "phase"},
            ExpressionAttributeValues={
                ":attempts": attempts + 1, ":now": int(now.timestamp()),
                ":dispatched": "dispatched"},
        )
        response = aws.client("lambda").invoke(
            FunctionName=LOADER, InvocationType="Event",
            Payload=json.dumps({
                "source": "phase6-" + slot, "run_id": previous["run_id"],
                "batch_id": previous["batch_id"]}).encode(),
        )
        if response.get("StatusCode") != 202:
            raise RuntimeError("Silver loader retry invocation was not accepted")
        return log("warehouse_loader_reinvoked", slot=slot, attempts=attempts + 1)
    owner = previous["owner"]
    renew(table, owner, int(now.timestamp()))
    if phase == "reserved":
        status = rds.describe_db_instances(DBInstanceIdentifier=RDS_ID)["DBInstances"][0]["DBInstanceStatus"]
        if status == "stopping":
            return log("rds_stopping", slot=slot, batch=previous["batch_id"])
        if status == "stopped":
            rds.start_db_instance(DBInstanceIdentifier=RDS_ID)
            return log("rds_start_requested", slot=slot, batch=previous["batch_id"])
        if status != "available":
            return log("waiting_for_rds", slot=slot, status=status)
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
            Key={"id": day_id},
            UpdateExpression="SET phase=:phase, run_id=:run",
            ConditionExpression="#day=:day AND #owner=:owner",
            ExpressionAttributeNames={"#day": "day", "#owner": "owner"},
            ExpressionAttributeValues={
                ":phase": "started", ":run": run_id, ":day": day, ":owner": owner,
            },
        )
        return log("glue_started", slot=slot, batch=batch, run_id=run_id)
    if phase == "started":
        run_id = previous["run_id"]
        state = glue.get_job_run(JobName=JOB, RunId=run_id)["JobRun"]["JobRunState"]
        if state in ACTIVE:
            return log("glue_running", slot=slot, run_id=run_id, state=state)
        if state not in FAILURES | {"SUCCEEDED"}:
            raise RuntimeError("Unexpected Glue state: " + state)
        if state == "SUCCEEDED":
            key = f"warehouse/shopify_snapshots/v1/{previous['batch_id']}/silver_manifest.json"
            aws.client("s3").head_object(Bucket=BUCKET, Key=key)
            next_phase = "dispatched"
        else:
            next_phase = "failed"
        # Persist dispatch phase BEFORE sending the async request: a fast
        # warehouse acknowledgment must never be overwritten as pending.
        release(table, owner)
        table.update_item(
            Key={"id": day_id},
            UpdateExpression=(
                "SET phase=:phase, glue_status=:status, "
                "loader_attempts=:attempts, loader_last_attempt_at=:when"
            ),
            ConditionExpression="#day=:day AND #owner=:owner",
            ExpressionAttributeNames={"#day": "day", "#owner": "owner"},
            ExpressionAttributeValues={
                ":phase": next_phase, ":status": state, ":day": day, ":owner": owner,
                ":attempts": 1 if state == "SUCCEEDED" else 0,
                ":when": int(now.timestamp()),
            },
        )
        if state == "SUCCEEDED":
            dispatched = aws.client("lambda").invoke(
                FunctionName=LOADER, InvocationType="Event",
                Payload=json.dumps({
                    "source": "phase6-" + slot, "run_id": run_id,
                    "batch_id": previous["batch_id"]}).encode(),
            )
            if dispatched.get("StatusCode") != 202:
                raise RuntimeError("Silver loader invocation was not accepted")
        return log(next_phase, slot=slot, run_id=run_id, glue_status=state)
    raise RuntimeError("Unknown Phase6 coordinator phase: " + str(phase))
