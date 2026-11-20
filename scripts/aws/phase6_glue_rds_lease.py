"""DynamoDB Glue->RDS lease. Does not start RDS or Glue.

All updates are owner-conditional on the EXISTING phase5-controller item.
Acquisition also requires the controller's 50s mutex to be free. This serializes
the reservation with its final RDS-stop decision, without extra AWS resources.

Do not activate until the deployed Phase5 controller image is Glue-lease aware.
"""
from __future__ import annotations

import argparse
import json
import time
from uuid import uuid4

import boto3
from botocore.exceptions import ClientError

TABLE = "ecommerce-demo-phase5-controller"
ITEM = {"id": "phase5-controller"}
JOB = "ecommerce-phase6-shopify-silver-demo"
ACTIVE = {"STARTING", "RUNNING", "STOPPING", "WAITING"}
TERMINAL = {"SUCCEEDED", "FAILED", "TIMEOUT", "STOPPED", "ERROR", "EXPIRED"}
TTL_SECONDS = 45 * 60


class LeaseBusy(RuntimeError):
    pass


def reserve(table, *, now: int, owner: str | None = None) -> str:
    """Reserve atomically with controller's stop mutex.

    An expired lease is deliberately NOT overwritten. Reconcile it first:
    expired does not prove that an abandoned Glue run has stopped.
    """
    owner = owner or str(uuid4())
    try:
        table.update_item(
            Key=ITEM,
            UpdateExpression=(
                "SET glue_lease_owner=:owner, glue_lease_expires_at=:expiry"
            ),
            ConditionExpression=(
                "(attribute_not_exists(lease_until) OR lease_until < :now) "
                "AND attribute_not_exists(glue_lease_owner)"
            ),
            ExpressionAttributeValues={
                ":owner": owner,
                ":expiry": now + TTL_SECONDS,
                ":now": now,
            },
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise LeaseBusy("Existing Glue lease or controller RDS stop mutex active") from exc
        raise
    return owner


def attach_run(table, *, owner: str, run_id: str, now: int) -> None:
    if not run_id.startswith("jr_"):
        raise ValueError("Expected AWS Glue job run ID")
    table.update_item(
        Key=ITEM,
        UpdateExpression=(
            "SET glue_lease_job_run_id=:run, glue_lease_expires_at=:expiry"
        ),
        ConditionExpression=(
            "glue_lease_owner=:owner AND glue_lease_expires_at > :now "
            "AND attribute_not_exists(glue_lease_job_run_id)"
        ),
        ExpressionAttributeValues={
            ":owner": owner, ":run": run_id,
            ":expiry": now + TTL_SECONDS, ":now": now,
        },
    )


def renew(table, *, owner: str, now: int) -> None:
    table.update_item(
        Key=ITEM,
        UpdateExpression="SET glue_lease_expires_at=:expiry",
        ConditionExpression="glue_lease_owner=:owner AND glue_lease_expires_at > :now",
        ExpressionAttributeValues={
            ":owner": owner, ":expiry": now + TTL_SECONDS, ":now": now,
        },
    )


def release(table, *, owner: str) -> None:
    table.update_item(
        Key=ITEM,
        UpdateExpression=(
            "REMOVE glue_lease_owner, glue_lease_expires_at, glue_lease_job_run_id"
        ),
        ConditionExpression="glue_lease_owner=:owner",
        ExpressionAttributeValues={":owner": owner},
    )


def is_safe_to_release(table, glue, *, owner: str) -> bool:
    """An unrecorded run ID is NOT safe for automatic lease release."""
    state = table.get_item(Key=ITEM, ConsistentRead=True).get("Item", {})
    if state.get("glue_lease_owner") != owner:
        raise LeaseBusy("Cannot release a lease owned by another runner")
    run_id = state.get("glue_lease_job_run_id")
    if not run_id:
        return False
    status = glue.get_job_run(JobName=JOB, RunId=run_id)["JobRun"]["JobRunState"]
    if status in ACTIVE:
        return False
    if status not in TERMINAL:
        raise RuntimeError("Unrecognized Glue status: " + status)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("reserve", "attach", "renew", "release"))
    parser.add_argument("--owner")
    parser.add_argument("--run-id")
    parser.add_argument("--profile", default="ecommerce-admin")
    parser.add_argument("--region", default="eu-central-1")
    args = parser.parse_args()
    if args.region != "eu-central-1":
        parser.error("Region is not the deployed project region")
    if args.action != "reserve" and not args.owner:
        parser.error("--owner required")
    if args.action == "attach" and not args.run_id:
        parser.error("--run-id required")
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    account = session.client("sts").get_caller_identity()["Account"]
    if account != "825414820081":
        raise RuntimeError("Unexpected AWS account")
    table = session.resource("dynamodb").Table(TABLE)
    glue = session.client("glue")
    now = int(time.time())
    if args.action == "reserve":
        owner = reserve(table, now=now, owner=args.owner)
        print(json.dumps({"owner": owner, "expiry_unix": now + TTL_SECONDS}))
    elif args.action == "attach":
        attach_run(table, owner=args.owner, run_id=args.run_id, now=now)
        print(json.dumps({"attached_run_id": args.run_id}))
    elif args.action == "renew":
        renew(table, owner=args.owner, now=now)
        print(json.dumps({"expiry_unix": now + TTL_SECONDS}))
    elif args.action == "release":
        if not is_safe_to_release(table, glue, owner=args.owner):
            raise LeaseBusy("Glue run not verified terminal; lease retained")
        release(table, owner=args.owner)
        print(json.dumps({"released": True}))


if __name__ == "__main__":
    main()
