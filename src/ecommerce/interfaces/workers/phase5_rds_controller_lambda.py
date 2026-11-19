"""Scheduled AWS Lambda controller (every minute + optional event invocation)."""
import json
import os
import boto3
from ecommerce.application.use_cases.rds_controller import RdsController, ControllerBusy


def handler(event, context):
    region = os.environ["AWS_REGION"]
    dd = boto3.resource("dynamodb", region_name=region)
    controller = RdsController(
        db=boto3.client("rds", region_name=region),
        ecs=boto3.client("ecs", region_name=region),
        index=dd.Table(os.environ["PENDING_EVENTS_TABLE"]),
        control=dd.Table(os.environ["CONTROLLER_TABLE"]),
        db_identifier=os.environ["DB_INSTANCE_IDENTIFIER"],
        cluster=os.environ["ECS_CLUSTER"],
        task_definition=os.environ["PROJECTOR_TASK_DEFINITION"],
        subnets=os.environ["PROJECTOR_SUBNETS"].split(","),
        security_groups=os.environ["PROJECTOR_SECURITY_GROUPS"].split(","),
        idle_minutes=int(os.environ.get("RDS_IDLE_MINUTES", "30")),
        automatic_stop_enabled=os.environ.get("ALLOW_AUTOMATIC_RDS_STOP", "false").lower() == "true",
    )
    try:
        action = controller.advance()
    except ControllerBusy:
        action = "controller_busy"
    print(json.dumps({"phase5_controller_action": action}))
    return {"action": action}
