"""Pure offline catalog reconciliation tests; no Shopify, AWS or RDS access."""
from contextlib import contextmanager
from uuid import uuid4

import pytest

from ecommerce.application.use_cases.catalog_selection import CatalogCandidate
from ecommerce.application.use_cases.catalog_sync import reconcile_catalog


def candidate():
    return CatalogCandidate(
        str(uuid4()), str(uuid4()), "85123A", "85123A", "Test item"
    )


class API:
    def __init__(self):
        self.products = {}
        self.variants = {}
        self.product_creates = 0
        self.variant_creates = 0
        self.fail_after_product_create_once = False

    def lookup_product(self, key):
        return self.products.get(key)

    def create_product(self, item):
        self.product_creates += 1
        self.products[item.product_id] = f"gid://shopify/Product/{self.product_creates}"
        if self.fail_after_product_create_once:
            self.fail_after_product_create_once = False
            raise TimeoutError("Ambiguous remote product mutation")
        return self.products[item.product_id]

    def lookup_variant(self, product_gid, sku):
        return self.variants.get((product_gid, sku))

    def create_variant(self, product_gid, item):
        self.variant_creates += 1
        gid = f"gid://shopify/ProductVariant/{self.variant_creates}"
        self.variants[(product_gid, item.sku)] = gid
        return gid


class Repo:
    def __init__(self):
        self.products = {}
        self.variants = {}

    def get_product(self, key):
        return self.products.get(key)

    def get_variant(self, key):
        return self.variants.get(key)

    def save_product(self, item, gid):
        old = self.products.get(item.product_id)
        if old and old != gid:
            raise ValueError("Product remapped")
        self.products[item.product_id] = gid

    def save_variant(self, item, product_gid, variant_gid):
        old = self.variants.get(item.variant_id)
        if old and old != (product_gid, variant_gid):
            raise ValueError("Variant remapped")
        self.variants[item.variant_id] = (product_gid, variant_gid)


class Lease:
    def __init__(self):
        self.claims = []

    @contextmanager
    def claim(self, product_id):
        self.claims.append(product_id)
        yield


def test_read_only_never_creates_or_persists():
    c, api, repo = candidate(), API(), Repo()
    result = reconcile_catalog([c], api, repo)
    assert result.missing_products == 1
    assert result.product_creates_requested == 0
    assert api.product_creates == 0
    assert repo.products == {}


def test_writes_require_durable_lease():
    with pytest.raises(ValueError, match="lease"):
        reconcile_catalog([candidate()], API(), Repo(), allow_write=True)


def test_product_and_variant_created_and_verified():
    c, api, repo, lease = candidate(), API(), Repo(), Lease()
    result = reconcile_catalog([c], api, repo, allow_write=True, lease=lease)
    assert result.product_creates_requested == 1
    assert result.variant_creates_requested == 1
    assert result.variant_mappings_verified == 1
    assert repo.get_variant(c.variant_id)[0] == repo.get_product(c.product_id)
    assert lease.claims == [c.product_id]


def test_restart_without_duplicate_remote_writes():
    c, api, repo, lease = candidate(), API(), Repo(), Lease()
    reconcile_catalog([c], api, repo, allow_write=True, lease=lease)
    second = reconcile_catalog([c], api, repo, allow_write=True, lease=lease)
    assert (api.product_creates, api.variant_creates) == (1, 1)
    assert (second.product_creates_requested, second.variant_creates_requested) == (0, 0)


def test_ambiguous_remote_mutation_requires_readback_before_retry():
    c, api, repo, lease = candidate(), API(), Repo(), Lease()
    api.fail_after_product_create_once = True
    with pytest.raises(TimeoutError):
        reconcile_catalog([c], api, repo, allow_write=True, lease=lease)
    assert repo.products == {}
    result = reconcile_catalog([c], api, repo, allow_write=True, lease=lease)
    assert result.product_creates_requested == 0
    assert api.product_creates == 1


def test_existing_mapping_mismatch_fails_closed_without_create():
    c, api, repo, lease = candidate(), API(), Repo(), Lease()
    repo.products[c.product_id] = "gid://shopify/Product/999"
    api.products[c.product_id] = "gid://shopify/Product/42"
    with pytest.raises(ValueError, match="conflicts"):
        reconcile_catalog([c], api, repo, allow_write=True, lease=lease)
    assert api.product_creates == 0


def test_variant_mismatch_fails_closed():
    c, api, repo, lease = candidate(), API(), Repo(), Lease()
    api.products[c.product_id] = "gid://shopify/Product/42"
    api.variants[("gid://shopify/Product/42", c.sku)] = "gid://shopify/ProductVariant/9"
    repo.variants[c.variant_id] = (
        "gid://shopify/Product/42", "gid://shopify/ProductVariant/8"
    )
    with pytest.raises(ValueError, match="variant mapping"):
        reconcile_catalog([c], api, repo, allow_write=True, lease=lease)


def test_missing_variant_read_only_is_observed_without_saving():
    c, api, repo = candidate(), API(), Repo()
    api.products[c.product_id] = "gid://shopify/Product/1"
    result = reconcile_catalog([c], api, repo)
    assert result.missing_variants == 1
    assert result.product_mappings_verified == 1
    assert repo.products == {}


def test_duplicate_candidate_rejected():
    c = candidate()
    with pytest.raises(ValueError, match="Duplicate product"):
        reconcile_catalog([c, c], API(), Repo())
