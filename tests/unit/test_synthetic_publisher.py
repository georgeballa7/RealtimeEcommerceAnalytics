"""Offline S3 commit and retry tests; no AWS resources are contacted."""
from datetime import UTC, datetime, timedelta
from hashlib import md5

import pytest
from botocore.exceptions import ClientError

from ecommerce.application.use_cases.synthetic_publisher import (
    DEFAULT_SYNTHETIC_PREFIX,
    deterministic_jsonl_gzip,
    publish_synthetic_interval,
)


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.fail_next_head_commit = False

    def put_object(self, *, Bucket, Key, Body, **options):
        if self.fail_next_head_commit and Key.endswith("/state/head.json"):
            self.fail_next_head_commit = False
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "CAS rejected"}},
                "PutObject",
            )
        current = self.objects.get(Key)
        if (options.get("IfNoneMatch") == "*" and current is not None) or (
            options.get("IfMatch") is not None
            and (
                current is None or self._etag(current) != options["IfMatch"]
            )
        ):
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "Already exists"}},
                "PutObject",
            )
        self.objects[Key] = Body

    @staticmethod
    def _etag(body):
        return f'"{md5(body, usedforsecurity=False).hexdigest()}"'

    def get_object(self, *, Bucket, Key):
        import io

        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "Missing"}}, "GetObject"
            )
        body = self.objects[Key]
        return {"Body": io.BytesIO(body), "ETag": self._etag(body)}


SHA = "b" * 64
ORIGIN = datetime(2026, 1, 1, tzinfo=UTC)


def publish(s3, start, end, orders=None, items=None):
    return publish_synthetic_interval(
        s3,
        bucket="demo",
        start_utc=start,
        cutoff_utc=end,
        orders=orders or [],
        items=items or [],
        policy_sha256=SHA,
        calibration_sha256=SHA,
        source_bronze_sha256=SHA,
        approved_catalog_sha256=SHA,
    )


def test_empty_interval_commit_is_replayable_and_contiguous():
    s3 = MemoryS3()
    first_cutoff = ORIGIN + timedelta(hours=12)
    manifest = publish(s3, ORIGIN, first_cutoff)
    assert manifest["start_utc"] == "2026-01-01T00:00:00Z"
    assert manifest["cutoff_utc"] == "2026-01-01T12:00:00Z"
    count = len(s3.objects)
    assert publish(s3, ORIGIN, first_cutoff) == manifest
    assert len(s3.objects) == count
    next_manifest = publish(s3, first_cutoff, first_cutoff + timedelta(hours=6))
    assert next_manifest["prior_manifest_key"].endswith("/manifest.json")
    with pytest.raises(ValueError, match="previous committed UTC cutoff"):
        publish(s3, ORIGIN, ORIGIN + timedelta(days=1))


def test_failed_checkpoint_leaves_only_uncommitted_staged_objects():
    s3 = MemoryS3()
    s3.fail_next_head_commit = True
    with pytest.raises(ClientError, match="PreconditionFailed"):
        publish(s3, ORIGIN, ORIGIN + timedelta(hours=2))
    assert f"{DEFAULT_SYNTHETIC_PREFIX}/state/head.json" not in s3.objects
    staged = len(s3.objects)
    assert staged == 3
    manifest = publish(s3, ORIGIN, ORIGIN + timedelta(hours=2))
    assert len(s3.objects) == 4
    assert manifest["files"]["orders"]["rows"] == 0


def test_order_ids_and_interval_bounds_checked_before_writes():
    s3 = MemoryS3()
    end = ORIGIN + timedelta(hours=1)
    event = ORIGIN + timedelta(minutes=1)
    order = {"order_id": "one", "event_time_utc": event.isoformat()}
    with pytest.raises(ValueError, match="Duplicate synthetic order IDs"):
        publish(s3, ORIGIN, end, [order, order])
    assert not s3.objects
    with pytest.raises(ValueError, match="outside its declared UTC interval"):
        publish(s3, ORIGIN, end, [{**order, "event_time_utc": end.isoformat()}])
    assert not s3.objects
    with pytest.raises(ValueError, match="Orphan"):
        publish(s3, ORIGIN, end, [order], [{"order_id": "missing", "order_item_id": "x"}])
    assert not s3.objects


def test_canonical_gzip_bytes_are_independent_of_invocation_time():
    rows = [{"a": 1, "z": "repeat"}, {"a": 2, "z": "repeat"}]
    assert deterministic_jsonl_gzip(rows) == deterministic_jsonl_gzip(rows)
