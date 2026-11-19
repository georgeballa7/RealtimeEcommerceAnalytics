"""FastAPI queue-first ingress unit tests; no AWS requests or RDS connection."""
import base64
import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from ecommerce.infrastructure.aws.webhook_queue import SqsWebhookQueue
from ecommerce.interfaces.http.shopify_webhooks import create_app

SHOP = "realtime-ecommerce-analytics.myshopify.com"
SECRET = "offline-test-secret"
BODY = b'{"id":123,"line_items":[]}'


class FakeSQS:
    def __init__(self):
        self.messages = []
        self.fail = False

    def send_message(self, **kwargs):
        if self.fail:
            raise RuntimeError("SQS failure")
        self.messages.append(json.loads(kwargs["MessageBody"]))
        return {"MessageId": "message-001"}


def headers(body=BODY, **overrides):
    h = {
        "X-Shopify-Hmac-Sha256": base64.b64encode(
            hmac.new(SECRET.encode(), body, hashlib.sha256).digest()).decode(),
        "X-Shopify-Webhook-Id": "webhook-001",
        "X-Shopify-Event-Id": "event-001",
        "X-Shopify-Topic": "orders/create",
        "X-Shopify-Shop-Domain": SHOP,
    }
    h.update(overrides)
    return h


def setup():
    sqs = FakeSQS()
    app = create_app(webhook_secret=SECRET,
                     expected_shop=SHOP,
                     queue=SqsWebhookQueue(sqs, "https://sqs.eu-central-1.amazonaws.com/123/queue"))
    return TestClient(app), sqs


def test_signed_webhook_is_queued_without_rds():
    client, sqs = setup()
    response = client.post("/webhooks/shopify", content=BODY, headers=headers())
    assert response.status_code == 200
    assert response.json() == {"status": "queued"}
    assert len(sqs.messages) == 1
    assert sqs.messages[0]["schema_version"] == 1
    assert sqs.messages[0]["source"] == "shopify_webhook"
    assert sqs.messages[0]["payload"]["id"] == 123


def test_invalid_signature_does_not_acknowledge():
    client, sqs = setup()
    response = client.post("/webhooks/shopify", content=b'{}', headers=headers())
    assert response.status_code == 401
    assert not sqs.messages


def test_other_shop_rejected():
    client, sqs = setup()
    response = client.post("/webhooks/shopify", content=BODY,
                           headers=headers(**{"X-Shopify-Shop-Domain": "another-store.myshopify.com"}))
    assert response.status_code == 403
    assert not sqs.messages


def test_sqs_failure_returns_503():
    client, sqs = setup()
    sqs.fail = True
    response = client.post("/webhooks/shopify", content=BODY, headers=headers())
    assert response.status_code == 503


def test_unsupported_topic_rejected():
    client, sqs = setup()
    response = client.post("/webhooks/shopify", content=BODY,
                           headers=headers(**{"X-Shopify-Topic": "customers/delete"}))
    assert response.status_code == 400
    assert not sqs.messages


def test_duplicate_delivery_can_be_queued_for_s3_dedupe():
    client, sqs = setup()
    for _ in range(2):
        assert client.post("/webhooks/shopify", content=BODY, headers=headers()).status_code == 200
    assert len(sqs.messages) == 2
    assert sqs.messages[0]["webhook_id"] == sqs.messages[1]["webhook_id"]
