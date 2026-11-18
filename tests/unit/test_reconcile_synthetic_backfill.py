"""Offline tests for the single-command, read-only S3 reconciliation workflow."""
from datetime import UTC, datetime, timedelta
from hashlib import md5

import pytest
from botocore.exceptions import ClientError

from ecommerce.application.use_cases.synthetic_publisher import publish_synthetic_interval
from ecommerce.application.use_cases.synthetic_reconciliation import reconcile

SHA = "b" * 64
START = datetime(2026, 1, 1, tzinfo=UTC)
GBP_ASSUMPTION = "GBP_assumed_from_UK_source_not_verified"
APPROVAL = (
    "historical/online_retail_ii/catalog_approval/v1/"
    "catalog-approval-unflagged-v1.json"
)


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, **kwargs):
        before = self.objects.get(Key)
        if (
            kwargs.get("IfNoneMatch") == "*" and before is not None
            or kwargs.get("IfMatch") is not None
            and (
                before is None or
                f'"{md5(before, usedforsecurity=False).hexdigest()}"' != kwargs["IfMatch"]
            )
        ):
            raise ClientError(
                {"Error": {"Code": "PreconditionFailed", "Message": "Exists"}},
                "PutObject",
            )
        self.objects[Key] = Body

    def get_object(self, *, Bucket, Key):
        import io

        if Key not in self.objects:
            raise ClientError(
                {"Error": {"Code": "NoSuchKey", "Message": "Missing"}},
                "GetObject",
            )
        raw = self.objects[Key]
        return {
            "Body": io.BytesIO(raw),
            "ETag": f'"{md5(raw, usedforsecurity=False).hexdigest()}"',
        }


def make_sample():
    s3 = FakeS3()
    # Isolated catalog fixture with its own pinned SHA.
    catalog = b'{"schema_version":"catalog-approval-v1","approved_stock_codes":["A"]}\n'
    import hashlib

    s3.objects[APPROVAL] = catalog
    catalog_sha = hashlib.sha256(catalog).hexdigest()
    order = {
        "order_id": "o-1", "customer_id": "c-1",
        "event_time_utc": (START + timedelta(hours=2)).isoformat(),
        "source_system": "synthetic_2026", "synthetic": True,
        "currency": "GBP",
        "order_total": "12.00",
    }
    item = {
        "order_item_id": "i-1", "order_id": "o-1",
        "product_id": "p-1", "variant_id": "v-1",
        "source_stock_code": "A", "quantity": 3,
        "unit_price": "4.00", "line_amount": "12.00",
        "event_time_utc": order["event_time_utc"],
        "synthetic": True, "currency": "GBP",
        "currency_assumption": GBP_ASSUMPTION,
    }
    cancellation = {
        "event_id": "e-cancel-1", "event_type": "order_item_cancelled",
        "order_id": "o-1", "order_item_id": "i-1", "customer_id": "c-1",
        "product_id": "p-1", "variant_id": "v-1",
        "source_stock_code": "A", "cancelled_quantity": 2,
        "unit_price": "4.00", "cancelled_amount": "8.00",
        "event_time_utc": (START + timedelta(days=1, hours=2)).isoformat(),
        "original_order_time_utc": order["event_time_utc"],
        "currency": "GBP",
        "synthetic": True, "provenance": "simulated_cancellation_not_verified_refund",
    }
    publish_synthetic_interval(
        s3, bucket="example", start_utc=START,
        cutoff_utc=START + timedelta(days=1),
        orders=[order], items=[item], cancellations=[],
        policy_sha256=SHA, calibration_sha256=SHA,
        source_bronze_sha256=SHA, approved_catalog_sha256=catalog_sha,
    )
    publish_synthetic_interval(
        s3, bucket="example", start_utc=START + timedelta(days=1),
        cutoff_utc=START + timedelta(days=2),
        orders=[], items=[], cancellations=[cancellation],
        policy_sha256=SHA, calibration_sha256=SHA,
        source_bronze_sha256=SHA, approved_catalog_sha256=catalog_sha,
    )
    return s3


def test_reconciliation_passing_published_v2_without_optional_order_event_metadata(monkeypatch):
    s3 = make_sample()
    from ecommerce.application.use_cases import synthetic_reconciliation as r

    # Supply a fixed fake allowlist and hash solely for this isolated test.
    catalog = b'{"schema_version":"catalog-approval-v1","approved_stock_codes":["A"]}\n'
    import hashlib

    monkeypatch.setattr(r, "APPROVAL_SHA256", hashlib.sha256(catalog).hexdigest())
    s3.objects[APPROVAL] = catalog
    result = reconcile(s3, "example")
    assert result["status"] == "PASS"
    assert result["committed_intervals"] == 2
    assert (result["orders"], result["order_items"], result["cancellations"]) == (1, 1, 1)
    assert result["gross_gbp"] == "12.00"
    assert result["cancelled_merchandise_gbp"] == "8.00"
    assert result["net_after_cancellations_gbp"] == "4.00"
    assert result["aws_writes"] is False


def test_reconciliation_fails_on_corrupt_committed_asset(monkeypatch):
    s3 = make_sample()
    from ecommerce.application.use_cases import synthetic_reconciliation as r

    catalog = b'{"schema_version":"catalog-approval-v1","approved_stock_codes":["A"]}\n'
    import hashlib

    monkeypatch.setattr(r, "APPROVAL_SHA256", hashlib.sha256(catalog).hexdigest())
    s3.objects[APPROVAL] = catalog
    key = next(key for key in s3.objects if key.endswith("/order_items.jsonl.gz"))
    s3.objects[key] += b"corrupted"
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        reconcile(s3, "example")


def test_no_committed_checkpoint_fails_closed():
    with pytest.raises(ClientError):
        reconcile(FakeS3(), "example")
