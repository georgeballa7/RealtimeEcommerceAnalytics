import pytest

from ecommerce.application.use_cases.identity_mapping import register_identity
from ecommerce.domain.models import SourceIdentity


class InMemoryIdentityRepository:
    def __init__(self) -> None:
        self.items: dict[tuple[str, str], SourceIdentity] = {}

    def get(self, source_system: str, source_id: str) -> SourceIdentity | None:
        return self.items.get((source_system, source_id))

    def save(self, identity: SourceIdentity) -> None:
        self.items[(identity.source_system, identity.source_id)] = identity


def test_register_identity_is_idempotent() -> None:
    repo = InMemoryIdentityRepository()
    identity = SourceIdentity("historical", "CST-00001", "cust_00001")
    assert register_identity(repo, identity) is True
    assert register_identity(repo, identity) is False


def test_conflicting_identity_is_rejected() -> None:
    repo = InMemoryIdentityRepository()
    register_identity(repo, SourceIdentity("historical", "CST-00001", "cust_00001"))
    with pytest.raises(ValueError):
        register_identity(repo, SourceIdentity("historical", "CST-00001", "cust_other"))
