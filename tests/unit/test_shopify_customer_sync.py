"""No network or database required."""
from uuid import uuid4

import httpx
import pytest

from ecommerce.application.use_cases.shopify_customer_sync import synchronize_customers
from ecommerce.infrastructure.postgres.customer_identity import HistoricalCustomer
from ecommerce.infrastructure.shopify.client import ShopifyConfig, ShopifyError, ShopifyGraphQLClient


class FakeAPI:
    def __init__(self):
        self.items = {}
        self.writes = 0

    def lookup_customer(self, key):
        return self.items.get(key)

    def upsert_customer(self, key):
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
        client.upsert_customer(str(uuid4()))
    assert len(attempts) == 1


def test_invalid_domain_rejected():
    with pytest.raises(ValueError):
        ShopifyConfig("evil.example.net", "secret")
