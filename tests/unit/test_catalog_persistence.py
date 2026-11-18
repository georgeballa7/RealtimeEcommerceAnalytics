"""Catalog identity and lease API boundaries without real PostgreSQL."""
from contextlib import contextmanager
from uuid import uuid4

import pytest

from ecommerce.application.use_cases.catalog_selection import CatalogCandidate
from ecommerce.infrastructure.postgres.catalog_identity import (
    PostgresCatalogIdentityRepository,
)
from ecommerce.infrastructure.postgres.catalog_lease import (
    CatalogLeaseBusy,
    PostgresCatalogLease,
)


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.response = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, query, params):
        self.connection.queries.append((query, params))
        if self.connection.busy and "RETURNING lease_token" in query:
            self.response = None
        elif "RETURNING" in query:
            self.response = (params[1],)
        else:
            self.response = None

    def fetchone(self):
        return self.response


class Connection:
    def __init__(self, *, autocommit=True, busy=False):
        self.autocommit = autocommit
        self.busy = busy
        self.queries = []

    @contextmanager
    def transaction(self):
        yield

    def cursor(self):
        return Cursor(self)


def candidate():
    return CatalogCandidate(
        str(uuid4()), str(uuid4()), "85123A", "85123A", "test merchandise"
    )


def test_mapping_requires_durable_transactions():
    with pytest.raises(ValueError, match="autocommit"):
        PostgresCatalogIdentityRepository(
            Connection(autocommit=False), "gid://shopify/Shop/1"
        )


def test_product_gid_must_be_real_shopify_type():
    repo = PostgresCatalogIdentityRepository(
        Connection(), "gid://shopify/Shop/1"
    )
    with pytest.raises(ValueError, match="Product"):
        repo.save_product(candidate(), "gid://shopify/Customer/3")
    assert repo.connection.queries == []


def test_variant_gid_must_not_be_product_gid():
    repo = PostgresCatalogIdentityRepository(
        Connection(), "gid://shopify/Shop/1"
    )
    with pytest.raises(ValueError, match="ProductVariant"):
        repo.save_variant(
            candidate(), "gid://shopify/Product/3", "gid://shopify/Product/3"
        )
    assert repo.connection.queries == []


def test_lease_claim_and_token_fenced_release():
    conn = Connection()
    lease = PostgresCatalogLease(conn, "gid://shopify/Shop/1")
    product_id = str(uuid4())
    with lease.claim(product_id):
        assert len(conn.queries) == 1
        assert conn.queries[0][1][1] == product_id
    assert len(conn.queries) == 2
    assert "lease_token=%s" in conn.queries[1][0]
    assert conn.queries[1][1][2] == conn.queries[0][1][2]


def test_concurrent_claim_is_rejected():
    conn = Connection(busy=True)
    lease = PostgresCatalogLease(conn, "gid://shopify/Shop/1")
    with pytest.raises(CatalogLeaseBusy):
        with lease.claim(str(uuid4())):
            pytest.fail("Should not acquire a busy lease")
    assert len(conn.queries) == 1


def test_lease_requires_autocommit():
    with pytest.raises(ValueError, match="Autocommit"):
        PostgresCatalogLease(
            Connection(autocommit=False), "gid://shopify/Shop/1"
        )
