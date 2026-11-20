import importlib.util
import hashlib
import json
from pathlib import Path
import pytest

p = Path(__file__).resolve().parents[2] / "scripts/generate_shopify_live_orders.py"
spec = importlib.util.spec_from_file_location("live_order_generator", p)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

POLICY = {"retail_probability_percent":90, "retail_line_cap":40,
          "wholesale_line_cap":70, "retail_units_per_line_cap":12,
          "wholesale_units_per_line_cap":100}

def test_deterministic_order_selection():
    variants = [{"id":f"gid://shopify/ProductVariant/{i}", "sku":f"S{i}", "price":"1.25"}
                for i in range(10)]
    customers = [f"gid://shopify/Customer/{i}" for i in range(100)]
    a = mod.make_orders(variants, customers, "daily-20261119", 150, 20261118, POLICY)
    b = mod.make_orders(variants, customers, "daily-20261119", 45, 20261118, POLICY)
    assert a == b
    assert len(a) == 150 and all(x["lines"] for x in a)

def test_price_drift_stops_without_mutation():
    p = [{"variants":{"nodes":[{"id":"v","sku":"A","price":"9.00"}],
                       "pageInfo":{"hasNextPage":False}}}]
    m = {"count":1,"prices":[{"sku":"A","price_gbp":"1.25"}]}
    with pytest.raises(RuntimeError,match="price drift"):
        mod.approved_variants(p,m)

class Journal:
    def __init__(self, record):
        self.record = record
    def read(self, index):
        return self.record
    def save(self, *args, **kwargs):
        raise AssertionError("No writes expected")

def test_reserved_ambiguous_state_fails_closed():
    order = {"index":0,"customer_id":"c","lines":[],"segment":"retail"}
    digest = hashlib.sha256(json.dumps(order, sort_keys=True).encode()).hexdigest()
    with pytest.raises(RuntimeError,match="reconcile manually"):
        mod.process_one(None, Journal({"status":"reserved","order_sha256":digest}), order, "daily-01")

def test_completed_state_is_idempotent():
    order = {"index":0,"customer_id":"c","lines":[],"segment":"retail"}
    digest = hashlib.sha256(json.dumps(order, sort_keys=True).encode()).hexdigest()
    assert mod.process_one(None, Journal({"status":"complete","order_sha256":digest}), order, "daily-01") == "already_complete"
