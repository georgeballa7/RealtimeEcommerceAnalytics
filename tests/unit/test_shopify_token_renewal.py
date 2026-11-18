"""Offline tests for Shopify client-credentials token renewal."""
import httpx
import pytest

from ecommerce.infrastructure.shopify.client import (
    ShopifyClientCredentials,
    ShopifyConfig,
    ShopifyError,
    ShopifyGraphQLClient,
)


def test_token_cached_and_renewed():
    now = [100.0]
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.path == "/admin/oauth/access_token"
        assert b"grant_type=client_credentials" in request.content
        return httpx.Response(200, json={"access_token": "test-token", "expires_in": 3600})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        provider = ShopifyClientCredentials(
            "demo.myshopify.com", "id", "secret", client=http, clock=lambda: now[0])
        assert provider.get_token() == "test-token"
        assert provider.get_token() == "test-token"
        assert len(requests) == 1
        now[0] += 3301
        assert provider.get_token() == "test-token"
        assert len(requests) == 2


def test_failure_does_not_expose_credentials():
    def handler(request):
        return httpx.Response(401, text="sensitive-body")

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        provider = ShopifyClientCredentials("demo.myshopify.com", "id", "private-secret", client=http)
        with pytest.raises(ShopifyError, match="token acquisition failed") as err:
            provider.get_token()
        assert "private-secret" not in str(err.value)
        assert "sensitive-body" not in str(err.value)


def test_read_only_401_renews_and_retries():
    calls = []
    def handler(request):
        calls.append(request.url.path)
        if request.url.path.endswith("/access_token"):
            return httpx.Response(200, json={"access_token": "test-token", "expires_in": 3600})
        if calls.count("/admin/api/2026-07/graphql.json") == 1:
            return httpx.Response(401)
        return httpx.Response(200, json={"data": {"shop": {
            "id": "gid://shopify/Shop/1", "myshopifyDomain": "demo.myshopify.com"}}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        provider = ShopifyClientCredentials("demo.myshopify.com", "id", "secret", client=http)
        with ShopifyGraphQLClient(ShopifyConfig("demo.myshopify.com"),
                                  client=http, token_provider=provider, sleep=lambda _: None) as api:
            assert api.verify_shop()[0] == "gid://shopify/Shop/1"
        assert calls.count("/admin/oauth/access_token") == 2


def test_mutation_401_never_replayed():
    writes = []
    def handler(request):
        if request.url.path.endswith("/access_token"):
            return httpx.Response(200, json={"access_token": "test-token", "expires_in": 3600})
        writes.append(request)
        return httpx.Response(401)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        provider = ShopifyClientCredentials("demo.myshopify.com", "id", "secret", client=http)
        with (
            ShopifyGraphQLClient(ShopifyConfig("demo.myshopify.com"),
                                 client=http, token_provider=provider) as api,
            pytest.raises(ShopifyError, match="authentication rejected"),
        ):
            api.query("mutation { test }", mutation=True)
        assert len(writes) == 1
