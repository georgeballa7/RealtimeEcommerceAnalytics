"""Offline webhook ingress tests: no AWS, Shopify or database connection required."""
import base64
import hashlib
import hmac

from fastapi.testclient import TestClient

from ecommerce.interfaces.http.shopify_webhooks import create_app, valid_hmac


class FakeInbox:
    def __init__(self):
        self.seen = set()
        self.events = []
        self.fail = False

    def accept(self, event):
        if self.fail:
            raise RuntimeError("database should not leak")
        key = (event.shop_domain, event.webhook_id)
        if key in self.seen:
            return False
        self.seen.add(key)
        self.events.append(event)
        return True


SECRET = "offline-test-only-secret"
BODY = b'{"id":123,"email":"do-not-log@example.test"}'


def signed_headers(body=BODY, *, webhook_id="example-123"):
    signature = base64.b64encode(
        hmac.new(SECRET.encode(), body, hashlib.sha256).digest()
    ).decode()
    return {
        "X-Shopify-Hmac-Sha256": signature,
        "X-Shopify-Webhook-Id": webhook_id,
        "X-Shopify-Topic": "orders/create",
        "X-Shopify-Shop-Domain": "realtime-ecommerce-analytics.myshopify.com",
        "X-Shopify-Event-Id": "event-123",
    }


def test_hmac_rejects_tampered_body():
    assert valid_hmac(BODY, signed_headers()["X-Shopify-Hmac-Sha256"], SECRET)
    assert not valid_hmac(b"tampered", signed_headers()["X-Shopify-Hmac-Sha256"], SECRET)


def test_accept_and_deduplicate_webhook():
    inbox = FakeInbox()
    client = TestClient(create_app(webhook_secret=SECRET, inbox=inbox))
    first = client.post("/webhooks/shopify", data=BODY, headers=signed_headers())
    repeat = client.post("/webhooks/shopify", data=BODY, headers=signed_headers())
    assert first.status_code == 200
    assert first.json() == {"status": "accepted"}
    assert repeat.json() == {"status": "duplicate"}
    assert len(inbox.events) == 1
    assert inbox.events[0].payload["id"] == 123


def test_invalid_signature_writes_nothing():
    inbox = FakeInbox()
    client = TestClient(create_app(webhook_secret=SECRET, inbox=inbox))
    response = client.post(
        "/webhooks/shopify", data=b'{"id":124}', headers=signed_headers()
    )
    assert response.status_code == 401
    assert not inbox.events


def test_missing_identity_rejected():
    inbox = FakeInbox()
    client = TestClient(create_app(webhook_secret=SECRET, inbox=inbox))
    headers = signed_headers()
    del headers["X-Shopify-Webhook-Id"]
    assert client.post("/webhooks/shopify", data=BODY, headers=headers).status_code == 400


def test_database_failure_must_not_acknowledge():
    inbox = FakeInbox()
    inbox.fail = True
    client = TestClient(create_app(webhook_secret=SECRET, inbox=inbox))
    result = client.post("/webhooks/shopify", data=BODY, headers=signed_headers())
    assert result.status_code == 503
    assert result.json() == {"detail": "Webhook storage unavailable"}


def test_oversized_payload_rejected():
    inbox = FakeInbox()
    client = TestClient(create_app(webhook_secret=SECRET, inbox=inbox))
    response = client.post(
        "/webhooks/shopify", data=b"x" * 1_048_577,
        headers=signed_headers(body=b"x" * 1_048_577),
    )
    assert response.status_code == 413
    assert not inbox.events
