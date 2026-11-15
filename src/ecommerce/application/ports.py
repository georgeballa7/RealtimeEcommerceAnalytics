"""Dependency-inversion interfaces implemented by infrastructure adapters."""
from typing import Protocol

from ecommerce.domain.models import EventMetadata, SourceIdentity


class IdentityRepository(Protocol):
    def get(self, source_system: str, source_id: str) -> SourceIdentity | None: ...

    def save(self, identity: SourceIdentity) -> None: ...


class EventInbox(Protocol):
    def accept_once(self, event: EventMetadata, payload: bytes) -> bool:
        """Atomically persist an event and its outbound publication intent."""
        ...


class EventPublisher(Protocol):
    def publish(self, event: EventMetadata, payload: bytes) -> None: ...
