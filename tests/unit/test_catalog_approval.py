"""Offline tests for the catalog ECS entrypoint's explicit approval boundary."""
import hashlib
import io
import json
from uuid import uuid4

import pytest

from ecommerce.application.use_cases.catalog_selection import CatalogCandidate
from ecommerce.application.use_cases.catalog_approval import approved_catalog, selected_catalog


class S3:
    def __init__(self, content: bytes):
        self.content = content

    def get_object(self, **kwargs):
        assert kwargs["Bucket"] == "test-bucket"
        assert kwargs["Key"].startswith("historical/online_retail_ii/catalog_approval/v1/")
        return {"Body": io.BytesIO(self.content)}


def candidate(code):
    return CatalogCandidate(
        str(uuid4()), str(uuid4()), code, code, "Reviewed product"
    )


def approval(codes):
    content = json.dumps({
        "schema_version": "catalog-approval-v1",
        "approved_stock_codes": codes,
    }).encode()
    return S3(content), hashlib.sha256(content).hexdigest()


KEY = "historical/online_retail_ii/catalog_approval/v1/approved.json"


def test_verified_approval_manifest():
    s3, digest = approval(["85123A", "85123B"])
    assert approved_catalog(s3, "test-bucket", KEY, digest) == {
        "85123A", "85123B"
    }


def test_invalid_checksum_fails_closed():
    s3, _ = approval(["85123A"])
    with pytest.raises(ValueError, match="checksum"):
        approved_catalog(s3, "test-bucket", KEY, "0" * 64)


def test_unreviewed_source_is_never_authorized():
    s3, digest = approval(["NOT_IN_SILVER"])
    approved = approved_catalog(s3, "test-bucket", KEY, digest)
    with pytest.raises(ValueError, match="unknown or ineligible"):
        selected_catalog([candidate("85123A")], limit=5, allow_write=True, approved=approved)


def test_write_requires_nonempty_approval():
    with pytest.raises(ValueError, match="reviewed catalog approvals"):
        selected_catalog([candidate("85123A")], limit=5, allow_write=True)


def test_approved_subset_used_for_writes_only():
    codes = [candidate("85123A"), candidate("85123B")]
    selected = selected_catalog(
        codes, limit=5, allow_write=True,
        approved=frozenset({"85123B"}),
    )
    assert [x.source_stock_code for x in selected] == ["85123B"]
    read_only = selected_catalog(codes, limit=5, allow_write=False)
    assert len(read_only) == 2


def test_invalid_approval_prefix_and_duplicates():
    s3, digest = approval(["85123A", "85123A"])
    with pytest.raises(ValueError, match="dedicated S3 prefix"):
        approved_catalog(s3, "test-bucket", "unreviewed.json", digest)
    with pytest.raises(ValueError, match="Duplicate"):
        approved_catalog(s3, "test-bucket", KEY, digest)
