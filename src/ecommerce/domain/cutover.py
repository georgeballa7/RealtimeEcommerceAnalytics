"""Half-open backfill/live time boundary for synthetic events."""
from dataclasses import dataclass
from datetime import datetime

from ecommerce.domain.models import DataOrigin


@dataclass(frozen=True, slots=True)
class CutoverPolicy:
    cutover_at_utc: datetime

    def __post_init__(self) -> None:
        if self.cutover_at_utc.tzinfo is None:
            raise ValueError("Cutover timestamp must be timezone-aware")

    def classify(self, event_time_utc: datetime) -> DataOrigin:
        if event_time_utc.tzinfo is None:
            raise ValueError("Event timestamp must be timezone-aware")
        return (
            DataOrigin.SYNTHETIC_BACKFILL
            if event_time_utc < self.cutover_at_utc
            else DataOrigin.SHOPIFY_LIVE
        )
