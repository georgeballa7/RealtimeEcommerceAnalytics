"""Offline SQS/S3 reliability tests. No cloud credentials required."""
import hashlib
import json

import pytest
from botocore.exceptions import ClientError

from ecommerce.application.use_cases.sqs_event_landing import (
    ConflictingReplay, InvalidEvent, SqsToS3LandingConsumer, WebhookEnvelope,
)


def message(*, payload=None, webhook_id="webhook-7"):
    body = {
        "schema_version": 1, "source": "shopify_webhook",
        "shop_domain": "example.myshopify.com", "webhook_id": webhook_id,
        "event_id": "event-7", "topic": "orders/create",
        "payload": {"id": 7} if payload is None else payload,
    }
    return {"MessageId": "sqs-7", "ReceiptHandle": "receipt-7", "Body": json.dumps(body)}


def error(code):
    return ClientError({"Error": {"Code": code, "Message": "private"}}, "PutObject")


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.failure = None

    def put_object(self, **kwargs):
        if self.failure:
            raise self.failure
        key = kwargs["Key"]
        if key in self.objects:
            raise error("PreconditionFailed")
        self.objects[key] = kwargs
        return {"ETag": "etag"}

    def head_object(self, **kwargs):
        return {"Metadata": self.objects[kwargs["Key"]]["Metadata"]}


class FakeSQS:
    def __init__(self, messages=None):
        self.messages = [] if messages is None else messages
        self.deleted = []

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs)

    def receive_message(self, **kwargs):
        return {"Messages": self.messages}


def worker(sqs, s3):
    return SqsToS3LandingConsumer(
        sqs_client=sqs, s3_client=s3,
        queue_url="https://sqs.eu-central-1.amazonaws.com/123/events",
        bucket="landing-test",
    )


def test_store_then_acknowledge():
    s3, sqs = FakeS3(), FakeSQS()
    assert worker(sqs, s3).process_message(message()) == "created"
    assert len(sqs.deleted) == 1
    stored = next(iter(s3.objects.values()))
    assert stored["IfNoneMatch"] == "*"
    assert stored["Key"].startswith("events/shopify/v1/")
    assert stored["Metadata"]["sha256"] == hashlib.sha256(stored["Body"]).hexdigest()


def test_duplicate_replay_acknowledged_only_after_hash_check():
    s3, sqs = FakeS3(), FakeSQS()
    consumer = worker(sqs, s3)
    assert consumer.process_message(message()) == "created"
    assert consumer.process_message(message()) == "duplicate"
    assert len(s3.objects) == 1
    assert len(sqs.deleted) == 2


def test_conflicting_replay_never_acknowledged():
    s3, sqs = FakeS3(), FakeSQS()
    consumer = worker(sqs, s3)
    consumer.process_message(message())
    with pytest.raises(ConflictingReplay):
        consumer.process_message(message(payload={"id": 8}))
    assert len(sqs.deleted) == 1


def test_transient_s3_failure_leaves_sqs_message():
    s3, sqs = FakeS3(), FakeSQS([message()])
    s3.failure = error("ServiceUnavailable")
    result = worker(sqs, s3).poll_once(wait_seconds=0, max_messages=1)
    assert result["failed"] == 1
    assert not sqs.deleted


def test_invalid_event_is_not_written_or_acknowledged():
    s3, sqs = FakeS3(), FakeSQS()
    event = message()
    body = json.loads(event["Body"])
    body["schema_version"] = 2
    event["Body"] = json.dumps(body)
    with pytest.raises(InvalidEvent):
        worker(sqs, s3).process_message(event)
    assert not s3.objects and not sqs.deleted


def test_idempotency_independent_of_json_order():
    one = WebhookEnvelope.from_sqs_body(message()["Body"])
    fields = json.loads(message()["Body"])
    two = WebhookEnvelope.from_sqs_body(json.dumps(dict(reversed(list(fields.items())))))
    assert one.object_key() == two.object_key()
    assert one.object_bytes() == two.object_bytes()


def test_poll_acks_only_successes():
    s3 = FakeS3()
    sqs = FakeSQS([message(), {
        "Body": "invalid", "MessageId": "bad", "ReceiptHandle": "bad-r"
    }])
    result = worker(sqs, s3).poll_once(wait_seconds=0)
    assert result == {"received": 2, "created": 1, "duplicate": 0, "failed": 1}
    assert len(sqs.deleted) == 1


def test_cross_shop_identity_keys_do_not_collide():
    first = json.loads(message()["Body"])
    second = dict(first, shop_domain="other.myshopify.com")
    assert WebhookEnvelope.from_sqs_body(json.dumps(first)).object_key() != (
        WebhookEnvelope.from_sqs_body(json.dumps(second)).object_key()
    )


def test_invalid_shop_rejected():
    fields = json.loads(message()["Body"])
    fields["shop_domain"] = "../example.myshopify.com"
    with pytest.raises(InvalidEvent):
        WebhookEnvelope.from_sqs_body(json.dumps(fields))


def test_invalid_poll_options():
    with pytest.raises(ValueError):
        worker(FakeSQS(), FakeS3()).poll_once(max_messages=11)
