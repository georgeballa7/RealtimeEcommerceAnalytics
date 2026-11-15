"""Core source-independent business identities and event envelope."""
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class DataOrigin(StrEnum):
    HISTORICAL = "historical"
    SYNTHETIC_BACKFILL = "synthetic_backfill"
    SHOPIFY_LIVE = "shopify_live"


@dataclass(frozen=True, slots=True)
class SourceIdentity:
    source_system: str
    source_id: str
    canonical_id: str

    def __post_init__(self) -> None:
        if not all((self.source_system, self.source_id, self.canonical_id)):
            raise ValueError("Identity fields must not be empty")


@dataclass(frozen=True, slots=True)
class EventMetadata:
    operation_id: str
    source_system: str
    source_record_id: str
    event_time_utc: datetime
    origin: DataOrigin

    def __post_init__(self) -> None:
        if not self.operation_id or not self.source_record_id or not self.source_system:
            raise ValueError("Event identifiers must not be empty")
        if self.event_time_utc.tzinfo is None:
            raise ValueError("Event timestamp must be timezone-aware")
