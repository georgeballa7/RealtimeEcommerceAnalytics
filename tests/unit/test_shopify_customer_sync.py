"""No network or database required."""
from uuid import uuid4

import httpx
import pytest

from ecommerce.application.use_cases.shopify_customer_sync import (
    customer_batches,
    synchronize_customers,
)
from ecommerce.infrastructure.postgres.customer_identity import HistoricalCustomer
from ecommerce.infrastructure.shopify.client import (
    ShopifyConfig,
    ShopifyError,
    ShopifyGraphQLClient,
)


class FakeAPI:
    def __init__(self):
        self.items = {}
        self.writes = 0

    def lookup_customer(self, key):
        return self.items.get(key)

    def upsert_customer(self, key, source_customer_id):
        self.writes += 1
        self.items[key] = "gid://shopify/Customer/123"
        return self.items[key]


class FakeRepo:
    def __init__(self):
        self.items = {}

    def get(self, key):
        return self.items.get(key)

    def save_verified(self, customer, gid):
        if gid in self.items.values() and self.items.get(customer.customer_id) != gid:
            raise ValueError("Customer GID reused")
        self.items[customer.customer_id] = gid


def test_dry_run_never_creates_or_saves():
    api, repo = FakeAPI(), FakeRepo()
    customer = HistoricalCustomer(str(uuid4()), "17850")
    summary = synchronize_customers([customer], api, repo)
    assert (summary.processed, summary.verified_new, api.writes, repo.items) == (1, 0, 0, {})


def test_write_then_retry_is_idempotent():
    api, repo = FakeAPI(), FakeRepo()
    customer = HistoricalCustomer(str(uuid4()), "17850")
    assert synchronize_customers([customer], api, repo, allow_write=True).verified_new == 1
    assert synchronize_customers([customer], api, repo, allow_write=True).skipped_existing == 1
    assert api.writes == 1


def test_lost_db_commit_is_reconciled_without_second_creation():
    api, repo = FakeAPI(), FakeRepo()
    customer = HistoricalCustomer(str(uuid4()), "17850")
    api.items[customer.customer_id] = "gid://shopify/Customer/123"
    assert synchronize_customers([customer], api, repo, allow_write=True).verified_new == 1
    assert api.writes == 0


def test_conflict_fails_closed():
    api, repo = FakeAPI(), FakeRepo()
    customer = HistoricalCustomer(str(uuid4()), "17850")
    repo.items[customer.customer_id] = "gid://shopify/Customer/999"
    with pytest.raises(ValueError):
        synchronize_customers([customer], api, repo, allow_write=True)


def test_graphql_query_and_custom_id_lookup():
    seen = []
    def respond(req):
        seen.append(req)
        import json
        assert req.headers["X-Shopify-Access-Token"] == "secret"
        payload = json.loads(req.content)
        assert payload["variables"]["identifier"]["customId"]["value"] == str(uid)
        return httpx.Response(200, json={"data": {"customerByIdentifier": {"id": "gid://shopify/Customer/42"}}})
    uid = uuid4()
    cfg = ShopifyConfig("test-shop.myshopify.com", "secret")
    client = ShopifyGraphQLClient(cfg, client=httpx.Client(transport=httpx.MockTransport(respond)))
    assert client.lookup_customer(str(uid)) == "gid://shopify/Customer/42"
    assert "/admin/api/2026-07/graphql.json" in str(seen[0].url)


def test_unsafe_graphql_mutation_transport_error_is_not_retried():
    attempts = []
    def fail(req):
        attempts.append(req)
        raise httpx.ConnectError("unavailable")
    cfg = ShopifyConfig("test-shop.myshopify.com", "secret")
    client = ShopifyGraphQLClient(cfg, client=httpx.Client(transport=httpx.MockTransport(fail)))
    with pytest.raises(ShopifyError):
        client.upsert_customer(str(uuid4()), "17850")
    assert len(attempts) == 1


def test_invalid_domain_rejected():
    with pytest.raises(ValueError):
        ShopifyConfig("evil.example.net", "secret")


def test_customer_batches_preserves_order_and_size():
    """Every customer is visited once; the last batch may be smaller."""
    customers = [HistoricalCustomer(str(uuid4()), str(i)) for i in range(205)]
    batches = list(customer_batches(iter(customers), 100))
    assert list(map(len, batches)) == [100, 100, 5]
    assert [customer for batch in batches for customer in batch] == customers


def test_customer_batches_rejects_invalid_sizes():
    customer = HistoricalCustomer(str(uuid4()), "1")
    for size in (0, -1, 1001):
        with pytest.raises(ValueError):
            list(customer_batches([customer], size))


def test_resume_after_partial_batch_failure_does_not_repeat_writes():
    """A failed run can restart from the beginning without another mutation."""
    class FailingOnceAPI(FakeAPI):
        def __init__(self):
            super().__init__()
            self.failed = False

        def lookup_customer(self, key):
            if not self.failed and len(self.items) == 3 and key not in self.items:
                self.failed = True
                raise RuntimeError("transient error")
            return super().lookup_customer(key)

    api, repo = FailingOnceAPI(), FakeRepo()
    # FakeRepo enforces unique Shopify GIDs; assign a fresh ID per customer.
    def upsert(key, source_customer_id):
        api.writes += 1
        api.items[key] = f"gid://shopify/Customer/{api.writes}"
        return api.items[key]
    api.upsert_customer = upsert
    customers = [HistoricalCustomer(str(uuid4()), str(i)) for i in range(7)]

    with pytest.raises(RuntimeError):
        for batch in customer_batches(customers, 5):
            synchronize_customers(batch, api, repo, allow_write=True)

    assert api.writes == 3
    assert len(repo.items) == 3

    for batch in customer_batches(customers, 5):
        synchronize_customers(batch, api, repo, allow_write=True)

    assert api.writes == 7
    assert len(repo.items) == 7


def test_shopify_validation_errors_include_safe_codes_and_fields_only():
    """Structured diagnostics are actionable without exposing error messages."""
    uid = str(uuid4())
    def respond(request):
        import json
        payload = json.loads(request.content)
        assert payload["variables"]["input"] == {"firstName": "HC17850"}
        return httpx.Response(200, json={"data": {"customerSet": {
            "customer": None,
            "userErrors": [
                {"code": "MISSING_FIELD_REQUIRED", "field": ["input", "email"],
                 "message": "Private customer@example.com"},
                {"code": "INPUT_MISMATCH", "field": ["identifier", "customId"],
                 "message": "Secret identifier value"},
            ],
        }}})
    cfg = ShopifyConfig("test-shop.myshopify.com", "secret")
    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
        ShopifyGraphQLClient(cfg, client=http) as api,
        pytest.raises(ShopifyError) as err,
    ):
        api.upsert_customer(uid, "17850")
    msg = str(err.value)
    assert "MISSING_FIELD_REQUIRED@input.email" in msg
    assert "INPUT_MISMATCH@identifier.customId" in msg
    assert "customer@example.com" not in msg
    assert "Secret identifier value" not in msg
    assert uid not in msg


def test_shopify_validation_diagnostics_reject_unsafe_fields_and_codes():
    """Unexpected metadata cannot bypass the redaction policy."""
    def respond(_request):
        return httpx.Response(200, json={"data": {"customerSet": {
            "customer": None,
            "userErrors": [
                {"code": "INVALID: private@example.com",
                 "field": ["input", "private@example.com"],
                 "message": "Do not log this value"},
                {"code": None, "field": "not a list",
                 "message": "Private value"},
            ],
        }}})
    cfg = ShopifyConfig("test-shop.myshopify.com", "secret")
    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
        ShopifyGraphQLClient(cfg, client=http) as api,
        pytest.raises(ShopifyError) as err,
    ):
        api.upsert_customer(str(uuid4()), "17850")
    assert "Shopify customerSet validation errors: UNKNOWN@input, UNKNOWN@unspecified" == str(err.value)
    assert "private@example.com" not in str(err.value)
    assert "Do not log" not in str(err.value)


def test_customer_set_uses_short_source_id_name_without_email():
    uid = str(uuid4())

    def respond(request):
        import json
        payload = json.loads(request.content)
        assert payload["variables"]["identifier"]["customId"]["value"] == uid
        assert payload["variables"]["input"] == {"firstName": "HC17850"}
        return httpx.Response(200, json={"data": {"customerSet": {
            "customer": {"id": "gid://shopify/Customer/42"},
            "userErrors": [],
        }}})

    cfg = ShopifyConfig("test-shop.myshopify.com", "secret")
    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
        ShopifyGraphQLClient(cfg, client=http) as api,
    ):
        assert api.upsert_customer(uid, "17850") == "gid://shopify/Customer/42"


def test_customer_set_rejects_invalid_source_id_before_mutation():
    cfg = ShopifyConfig("test-shop.myshopify.com", "secret")

    def unexpected(_request):
        pytest.fail("Invalid source ID must never reach Shopify")

    with (
        httpx.Client(transport=httpx.MockTransport(unexpected)) as http,
        ShopifyGraphQLClient(cfg, client=http) as api,
        pytest.raises(ValueError, match="numeric historical source"),
    ):
        api.upsert_customer(str(uuid4()), "not-an-id")


def test_graphql_errors_report_only_safe_code_and_schema_field():
    """GraphQL top-level errors are diagnosed without exposing customer data."""
    def respond(_request):
        return httpx.Response(200, json={"errors": [
            {
                "message": "Field 'firstName' doesn't exist on type 'CustomerSetInput'",
                "extensions": {"code": "GRAPHQL_VALIDATION_FAILED"},
            },
            {
                "message": "Sensitive value private@example.com with token abc123",
                "extensions": {"code": "ACCESS_DENIED"},
            },
        ]})

    cfg = ShopifyConfig("test-shop.myshopify.com", "secret")
    with (
        httpx.Client(transport=httpx.MockTransport(respond)) as http,
        ShopifyGraphQLClient(cfg, client=http) as api,
        pytest.raises(ShopifyError) as err,
    ):
        api.upsert_customer(str(uuid4()), "17850")
    message = str(err.value)
    assert "GRAPHQL_VALIDATION_FAILED@CustomerSetInput.firstName" in message
    assert "ACCESS_DENIED" in message
    assert "private@example.com" not in message
    assert "abc123" not in message
