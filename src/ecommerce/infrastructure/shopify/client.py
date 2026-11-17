"""Shopify GraphQL Admin API transport and custom-ID customer operations.

Credentials and shop domain are injected; never log a token or response body.
Shopify writes are only issued from explicit sync calls, not from construction.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable

import httpx


class ShopifyError(RuntimeError):
    """An API, protocol or user-level error; no automatic unsafe retry."""


@dataclass(frozen=True)
class ShopifyConfig:
    shop_domain: str
    token: str
    api_version: str = "2026-07"
    metafield_namespace: str = "custom"
    metafield_key: str = "ecommerce_canonical_customer_id"

    def __post_init__(self):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*\.myshopify\.com", self.shop_domain):
            raise ValueError("Expected a myshopify.com shop domain, not an arbitrary URL")
        if not self.token.strip():
            raise ValueError("Shopify token is missing")
        if not re.fullmatch(r"20\d{2}-(01|04|07|10)", self.api_version):
            raise ValueError("Invalid Shopify API version")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.metafield_namespace):
            raise ValueError("Invalid metafield namespace")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", self.metafield_key):
            raise ValueError("Invalid metafield key")


SHOP_QUERY = "query ShopIdentity { shop { id myshopifyDomain } }"
DEFINITION_QUERY = """query CustomerIdDefinition($namespace: String!, $key: String!) {
  metafieldDefinitions(first: 50, ownerType: CUSTOMER, namespace: $namespace, key: $key) {
    nodes { id namespace key type { name } }
  }
}"""
CUSTOMER_SET_GENERIC = """mutation UpsertHistoricalCustomer($identifier: CustomerSetIdentifiers!, $input: CustomerSetInput!) {
  customerSet(identifier: $identifier, input: $input) {
    customer { id }
    userErrors { field message code }
  }
}"""
CUSTOMER_BY_CUSTOM_ID = """query CustomerByCanonicalId($identifier: CustomerIdentifierInput!) {
  customerByIdentifier(identifier: $identifier) { id }
}"""


class ShopifyGraphQLClient:
    def __init__(self, config: ShopifyConfig, *, client: httpx.Client | None = None,
                 sleep: Callable[[float], None] = time.sleep, max_attempts: int = 4):
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.config = config
        self._client = client or httpx.Client(timeout=30.0)
        self._owns_client = client is None
        self._sleep = sleep
        self.max_attempts = max_attempts

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def close(self):
        if self._owns_client:
            self._client.close()

    def query(self, document: str, variables: dict | None = None, *, mutation: bool = False) -> dict:
        url = f"https://{self.config.shop_domain}/admin/api/{self.config.api_version}/graphql.json"
        # Mutations are NOT automatically retried on ambiguous timeouts/5xx.
        # Only explicit Shopify throttle/429 responses are safe to replay because
        # customerSet is keyed on the unique canonical customer ID.
        for attempt in range(self.max_attempts):
            try:
                resp = self._client.post(
                    url, json={"query": document, "variables": variables or {}},
                    headers={"X-Shopify-Access-Token": self.config.token,
                             "Content-Type": "application/json"})
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if mutation or attempt == self.max_attempts - 1:
                    raise ShopifyError("Network failure; reconcile custom ID before retry") from exc
                self._sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code == 429 and attempt < self.max_attempts - 1:
                retry_header = resp.headers.get("Retry-After", "1")
                try:
                    delay = max(1.0, min(float(retry_header), 30.0))
                except ValueError:
                    delay = 1.0
                self._sleep(delay)
                continue
            if resp.status_code in (500, 502, 503, 504) and not mutation and attempt < self.max_attempts - 1:
                self._sleep(min(2 ** attempt, 8))
                continue
            if resp.status_code != 200:
                raise ShopifyError(f"Shopify HTTP status {resp.status_code}; response withheld")
            try:
                body = resp.json()
            except ValueError as exc:
                raise ShopifyError("Non-JSON Shopify response") from exc
            if not isinstance(body, dict):
                raise ShopifyError("Malformed Shopify JSON")
            if body.get("errors"):
                raise ShopifyError("Shopify GraphQL errors; check permissions and request schema")
            if not isinstance(body.get("data"), dict):
                raise ShopifyError("Missing GraphQL data")
            return body["data"]
        raise ShopifyError("Shopify throttled all attempts")

    def verify_shop(self) -> tuple[str, str]:
        shop = self.query(SHOP_QUERY).get("shop") or {}
        gid, domain = shop.get("id"), shop.get("myshopifyDomain")
        if not isinstance(gid, str) or not gid.startswith("gid://shopify/Shop/"):
            raise ShopifyError("Invalid Shopify shop ID")
        if domain != self.config.shop_domain:
            raise ShopifyError("Shop domain mismatch")
        return gid, domain

    def verify_custom_id_definition(self) -> None:
        defs = self.query(DEFINITION_QUERY, {"namespace": self.config.metafield_namespace,
                                             "key": self.config.metafield_key})
        nodes = ((defs.get("metafieldDefinitions") or {}).get("nodes") or [])
        valid = [d for d in nodes if d.get("namespace") == self.config.metafield_namespace
                 and d.get("key") == self.config.metafield_key
                 and (d.get("type") or {}).get("name") == "id"]
        if len(valid) != 1:
            raise ShopifyError("Expected exactly one CUSTOMER custom-ID metafield definition of type id")

    def lookup_customer(self, canonical_id: str) -> str | None:
        identity = self._custom_id(canonical_id)
        customer = self.query(CUSTOMER_BY_CUSTOM_ID, {"identifier": {"customId": identity}}).get("customerByIdentifier")
        return self._validate_gid(customer.get("id")) if customer else None

    def upsert_customer(self, canonical_id: str) -> str:
        result = self.query(CUSTOMER_SET_GENERIC,
                            {"identifier": {"customId": self._custom_id(canonical_id)},
                             "input": {}}, mutation=True).get("customerSet") or {}
        if result.get("userErrors"):
            raise ShopifyError("Shopify customerSet returned validation errors")
        customer = result.get("customer") or {}
        return self._validate_gid(customer.get("id"))

    def _custom_id(self, canonical_id: str) -> dict:
        from uuid import UUID
        try:
            if str(UUID(canonical_id)) != canonical_id:
                raise ValueError("non-canonical UUID")
        except (ValueError, AttributeError) as exc:
            raise ValueError("Expected canonical customer UUID") from exc
        return {"namespace": self.config.metafield_namespace,
                "key": self.config.metafield_key, "value": canonical_id}

    @staticmethod
    def _validate_gid(value: object) -> str:
        if not isinstance(value, str) or not re.fullmatch(r"gid://shopify/Customer/[0-9]+", value):
            raise ShopifyError("Invalid Shopify customer GID")
        return value
