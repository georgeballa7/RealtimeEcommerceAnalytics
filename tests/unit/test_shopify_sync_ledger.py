"""No AWS, Shopify or PostgreSQL required: verify event ordering/retry safety."""
from contextlib import contextmanager
from uuid import uuid4
import pytest

from ecommerce.application.use_cases.shopify_customer_sync import synchronize_customers
from ecommerce.infrastructure.postgres.customer_identity import HistoricalCustomer


class Ledger:
    def __init__(self):
        self.events = []
        self.claims = []

    @contextmanager
    def claim(self, customer_id):
        self.claims.append(customer_id)
        yield

    def record(self, customer_id, event):
        self.events.append((customer_id, event.phase, event.outcome, event.failure_code))


class Repo:
    def __init__(self):
        self.items = {}

    def get(self, key):
        return self.items.get(key)

    def save_verified(self, customer, gid):
        self.items[customer.customer_id] = gid


class API:
    def __init__(self):
        self.items = {}
        self.writes = 0
        self.timeout_once = False

    def lookup_customer(self, cid):
        return self.items.get(cid)

    def upsert_customer(self, cid):
        self.writes += 1
        self.items[cid] = "gid://shopify/Customer/987"
        if self.timeout_once:
            self.timeout_once = False
            raise TimeoutError("ambiguous mutation")
        return self.items[cid]


def customer():
    return HistoricalCustomer(str(uuid4()), "17850")


def test_read_only_never_creates_remote_customer():
    ledger, repo, api, c = Ledger(), Repo(), API(), customer()
    result = synchronize_customers([c], api, repo, ledger=ledger)
    assert result.verified_new == 0 and api.writes == 0
    assert [(p, o) for _, p, o, _ in ledger.events] == [
        ("lookup", "started"), ("lookup", "not_found")]


def test_mutation_audited_before_shopify_and_mapping_persisted():
    ledger, repo, api, c = Ledger(), Repo(), API(), customer()
    summary = synchronize_customers([c], api, repo, ledger=ledger, allow_write=True)
    assert summary.verified_new == 1 and api.writes == 1
    assert [(p, o) for _, p, o, _ in ledger.events] == [
        ("lookup", "started"), ("lookup", "not_found"),
        ("upsert", "started"), ("reconcile", "started"),
        ("reconcile", "verified"), ("persist", "started"), ("persist", "verified")]
    assert repo.items[c.customer_id] == api.items[c.customer_id]


def test_ambiguous_mutation_reconciles_without_duplicate_write():
    ledger, repo, api, c = Ledger(), Repo(), API(), customer()
    api.timeout_once = True
    with pytest.raises(TimeoutError):
        synchronize_customers([c], api, repo, ledger=ledger, allow_write=True)
    assert api.writes == 1
    assert ledger.events[-1][2:] == ("ambiguous", "RECONCILE_REQUIRED")
    result = synchronize_customers([c], api, repo, ledger=ledger, allow_write=True)
    assert result.verified_new == 1 and api.writes == 1


def test_existing_mapping_is_checked_against_shopify():
    ledger, repo, api, c = Ledger(), Repo(), API(), customer()
    repo.items[c.customer_id] = "gid://shopify/Customer/987"
    api.items[c.customer_id] = "gid://shopify/Customer/987"
    assert synchronize_customers([c], api, repo, ledger=ledger, allow_write=True).skipped_existing == 1
    assert api.writes == 0


def test_remote_conflict_fails_closed():
    ledger, repo, api, c = Ledger(), Repo(), API(), customer()
    repo.items[c.customer_id] = "gid://shopify/Customer/123"
    api.items[c.customer_id] = "gid://shopify/Customer/987"
    with pytest.raises(ValueError, match="Stored mapping"):
        synchronize_customers([c], api, repo, ledger=ledger, allow_write=True)
    assert api.writes == 0 and ledger.events[-1][2] == "failed"
