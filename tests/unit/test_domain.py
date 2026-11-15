from datetime import UTC, datetime

import pytest

from ecommerce.domain.cutover import CutoverPolicy
from ecommerce.domain.models import DataOrigin, EventMetadata, SourceIdentity


def test_backfill_boundary_is_exclusive() -> None:
    boundary = datetime(2026, 11, 15, 16, tzinfo=UTC)
    policy = CutoverPolicy(boundary)
    assert policy.classify(datetime(2026, 7, 1, tzinfo=UTC)) == DataOrigin.SYNTHETIC_BACKFILL
    assert policy.classify(boundary) == DataOrigin.SHOPIFY_LIVE


def test_source_identity_requires_values() -> None:
    with pytest.raises(ValueError):
        SourceIdentity(source_system="historical", source_id="", canonical_id="c1")


def test_event_timestamp_must_be_aware() -> None:
    with pytest.raises(ValueError):
        EventMetadata(
            operation_id="op1",
            source_system="shopify",
            source_record_id="1",
            event_time_utc=datetime(2026, 11, 15),
            origin=DataOrigin.SHOPIFY_LIVE,
        )
