"""Pure fake-DB tests for the guarded migration 007 executor."""
from pathlib import Path

import pytest

from scripts.apply_phase5_price_migration import run


class FakeCursor:
    def __init__(self, *, table_exists=False, history=None):
        self.table_exists = table_exists
        self.history = history
        self.result = None
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, statement, params=None, **kwargs):
        query = str(statement)
        self.calls.append(query)
        if "to_regclass('public.shopify_product_identity')" in query:
            self.result = ("products", "variants",
                           "price" if self.table_exists else None,
                           "schema_migrations" if self.history else None)
        elif "FROM pg_roles" in query:
            self.result = (1,)
        elif "FROM schema_migrations" in query:
            self.result = self.history
        elif "has_table_privilege" in query:
            self.result = (True,)
        elif "to_regclass('public.shopify_variant_price_state')" in query:
            self.result = ("price",)
        elif "INSERT INTO schema_migrations" in query:
            self.table_exists = True
            self.result = None
        else:
            self.result = None

    def fetchone(self):
        return self.result


class FakeConnection:
    def __init__(self, **kwargs):
        self.cur = FakeCursor(**kwargs)

    def transaction(self):
        return self

    def cursor(self):
        return self.cur

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


SQL = (Path(__file__).resolve().parents[2] /
       "infrastructure/sql/007_shopify_variant_price_state.sql").read_text()


def test_preflight_is_read_only():
    c = FakeConnection()
    assert run(connection=c, apply=False, migration_sql=SQL, app_role="ecommerce_app") == "pending"
    assert all("CREATE TABLE" not in query and "GRANT" not in query for query in c.cur.calls)


def test_guard_rejects_wrong_app_role():
    with pytest.raises(ValueError):
        run(connection=FakeConnection(), apply=True, migration_sql=SQL, app_role="postgres")


def test_ambiguous_untracked_price_table_fails_closed():
    with pytest.raises(RuntimeError, match="without migration 007 record"):
        run(connection=FakeConnection(table_exists=True), apply=True,
            migration_sql=SQL, app_role="ecommerce_app")
