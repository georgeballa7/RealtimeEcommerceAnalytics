"""Pure SQL result validation for Phase 5 read-only audit."""
from datetime import datetime, timezone
from decimal import Decimal

from ecommerce.application.use_cases.phase5_price_audit import audit_price_state


class Cursor:
    def __init__(self, rows, n):
        self.rows = rows
        self.count = n
        self.query_index = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, query):
        assert query.strip().startswith("SELECT")
        self.query_index += 1

    def fetchall(self):
        return self.rows

    def fetchone(self):
        return (self.count,)


class Conn:
    def __init__(self, rows, count):
        self.rows, self.count = rows, count

    def cursor(self):
        return Cursor(self.rows, self.count)


def test_audit_all_approved_rows_match():
    digest = "a" * 64
    prices = [{"sku": str(i), "canonical_variant_id": f"uuid-{i}",
               "price_gbp": "1.25"} for i in range(3408)]
    rows = [(str(i), f"uuid-{i}", "1.25", "GBP", digest,
             datetime.now(timezone.utc)) for i in range(3408)]
    result = audit_price_state(Conn(rows, 3408), {"count": 3408, "prices": prices}, digest)
    assert result["matched"] == 3408
    assert result["errors"] == 0


def test_audit_detects_missing_price():
    digest = "a" * 64
    prices = [{"sku": str(i), "canonical_variant_id": f"uuid-{i}",
               "price_gbp": "1.25"} for i in range(3408)]
    rows = [(str(i), f"uuid-{i}", "1.25" if i else None, "GBP", digest,
             datetime.now(timezone.utc)) for i in range(3408)]
    result = audit_price_state(Conn(rows, 3407), {"count": 3408, "prices": prices}, digest)
    assert result["matched"] == 3407
    assert result["errors"] >= 2
