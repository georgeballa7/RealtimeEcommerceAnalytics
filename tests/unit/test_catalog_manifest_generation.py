"""Unflagged catalog approval is deterministic and rejects incomplete audit data."""
import json

import pytest

from ecommerce.application.use_cases.catalog_approval import manifest_from_audit


def report():
    return {
        "source": {"bronze_source_sha256": "f" * 64},
        "source_products": 4,
        "candidate_products": 3,
        "excluded_by_class": {"shipping_or_fee": 1},
        "manual_review_required": 1,
        "without_detected_flags": 2,
        "review_queue": [
            {"stock_code": "85123B", "needs_manual_review": False, "review_reasons": []},
            {
                "stock_code": "85123A",
                "needs_manual_review": True,
                "review_reasons": ["multiple_source_descriptions"],
            },
            {"stock_code": "85123C", "needs_manual_review": False, "review_reasons": []},
        ],
    }


def test_manifest_contains_exactly_unflagged_codes():
    payload = manifest_from_audit(report())
    assert json.loads(payload) == {
        "schema_version": "catalog-approval-v1",
        "approved_stock_codes": ["85123B", "85123C"],
    }
    assert b"85123A" not in payload
    assert payload == manifest_from_audit(report())


def test_inconsistent_totals_rejected():
    r = report()
    r["without_detected_flags"] = 3
    with pytest.raises(ValueError, match="reconcile"):
        manifest_from_audit(r)


def test_conflicting_row_flags_rejected():
    r = report()
    r["review_queue"][0]["needs_manual_review"] = True
    with pytest.raises(ValueError, match="Conflicting"):
        manifest_from_audit(r)


def test_duplicate_sku_rejected():
    r = report()
    r["review_queue"][2]["stock_code"] = "85123B"
    with pytest.raises(ValueError, match="reconcile"):
        manifest_from_audit(r)


def test_missing_full_review_queue_rejected():
    r = report()
    r["review_queue"] = r["review_queue"][:2]
    with pytest.raises(ValueError, match="reconcile"):
        manifest_from_audit(r)
