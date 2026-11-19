"""SQS-triggered Lambda: land and index before acknowledging SQS.

After durable persistence, asynchronously wake the RDS controller. Retry if the
wakeup cannot be accepted: replays are safe because S3 and the index dedupe.
A periodic controller tick additionally protects against invocation loss.
"""
import os
import json
import boto3
from ecommerce.application.use_cases.sqs_event_landing import SqsToS3LandingConsumer, WebhookEnvelope
from ecommerce.infrastructure.aws.event_index import index_landed_event, signal_activity


def handler(event, context):
    bucket = os.environ["S3_LANDING_BUCKET"]
    table_name = os.environ["PENDING_EVENTS_TABLE"]
    consumer = SqsToS3LandingConsumer(sqs_client=None, s3_client=boto3.client("s3"),
                                      queue_url=os.environ["EVENT_QUEUE_URL"], bucket=bucket)
    table = boto3.resource("dynamodb").Table(table_name)
    control = boto3.resource("dynamodb").Table(os.environ["CONTROLLER_TABLE"])
    failures = []
    controller_name = os.environ["RDS_CONTROLLER_FUNCTION"]
    invoker = boto3.client("lambda")
    for record in event.get("Records", []):
        try:
            envelope = WebhookEnvelope.from_sqs_body(record["body"])
            consumer.persist(envelope)
            import hashlib
            checksum = hashlib.sha256(envelope.object_bytes()).hexdigest()
            index_landed_event(table, shop_domain=envelope.shop_domain,
                               webhook_id=envelope.webhook_id, s3_key=envelope.object_key(),
                               bucket=bucket, checksum=checksum)
            signal_activity(control)
            # Asynchronous wakeup must be accepted before SQS can acknowledge.
            # If the worker is already running, the singleton controller lock
            # and durable pending index make this a harmless redundant signal.
            response = invoker.invoke(
                FunctionName=controller_name, InvocationType="Event",
                Payload=json.dumps({"source": "phase5_s3_landing"}).encode("utf-8"),
            )
            if response.get("StatusCode") != 202:
                raise RuntimeError("Controller wakeup not accepted")
        except Exception:
            failures.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": failures}
