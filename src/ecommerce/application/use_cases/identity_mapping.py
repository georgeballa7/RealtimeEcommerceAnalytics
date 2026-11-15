"""Source identity registration with conflict detection."""
from ecommerce.application.ports import IdentityRepository
from ecommerce.domain.models import SourceIdentity


def register_identity(repository: IdentityRepository, identity: SourceIdentity) -> bool:
    """Return False for a consistent retry; reject conflicting remaps."""
    existing = repository.get(identity.source_system, identity.source_id)
    if existing is not None:
        if existing != identity:
            raise ValueError("Source identity already maps to a different canonical ID")
        return False
    repository.save(identity)
    return True
