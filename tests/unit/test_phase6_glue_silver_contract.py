"""Local Spark validation of Phase 6 Silver contract, without AWS access."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

pyspark = pytest.importorskip("pyspark")
from pyspark.sql import SparkSession

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / "scripts/glue/phase6_shopify_silver.py"
DDL = ROOT / "analytics/redshift/silver_staging_schema.sql"

spec = importlib.util.spec_from_file_location("phase6_shopify_silver", MODULE)
assert spec and spec.loader
glue = importlib.util.module_from_spec(spec)
spec.loader.exec_module(glue)


@pytest.fixture(scope="session")
def spark():
    session = (
        SparkSession.builder.master("local[1]")
        .appName("phase6-silver-unit-tests")
        .config("spark.ui.enabled", "false")
        .config("spark.sql.session.timeZone", "UTC")
        .getOrCreate()
    )
    yield session
    session.stop()


def source_row(table):
    base = {
        "shop_domain": "demo.myshopify.com", "shopify_order_id": 123,
        "shopify_customer_gid": "gid://shopify/Customer/7",
        "currency": "GBP", "created_at_source": "2026-11-20T08:00:00Z",
        "updated_at_source": "2026-11-20T08:00:00Z",
        "cancelled_at": None, "financial_status": "paid",
        "fulfillment_status": None, "gross_total": "20.00",
        "current_total": "20.00", "synthetic": True,
        "ingested_at": "2026-11-20T08:01:00Z",
        "shopify_line_id": 99, "shopify_variant_id": 55,
        "sku": "SKU-ABC", "quantity": 2, "unit_price": "10.00",
        "discount_total": "0.00", "shopify_refund_id": 9,
        "refund_line_subtotal": "5.00", "payload_sha256": "a" * 64,
        "shopify_refund_line_id": 19, "shopify_order_line_id": 99,
        "subtotal": "5.00", "shop_gid": "gid://shopify/Shop/1",
        "customer_id": "123e4567-e89b-12d3-a456-426614174000",
        "source_system": "online_retail_ii", "source_customer_id": "17850",
        "status": "verified", "updated_at": "2026-11-20T08:02:00Z",
    }
    return {name: base[name] for name, _ in glue.CONTRACT[table]}


@pytest.mark.parametrize("table", list(glue.CONTRACT))
def test_source_to_silver_contract(spark, table):
    row = source_row(table)
    schema = ", ".join(f"`{name}` string" for name in row)
    source = spark.createDataFrame([tuple(row.values())], schema=schema)
    cleaned = glue.add_reasons(glue.to_contract(source, table), table)
    assert cleaned.where("size(dq_reasons) > 0").count() == 0
    assert list(cleaned.columns) == list(row) + ["dq_reasons"]
    glue.ensure_unique(cleaned, glue.TABLE_KEYS[table], table)


def test_overlong_domain_goes_to_quarantine(spark):
    row = source_row("shopify_live_order")
    row["shop_domain"] = "x" * 256
    source = spark.createDataFrame([tuple(row.values())],
                                   schema=", ".join(f"`{n}` string" for n in row))
    failed = glue.add_reasons(glue.to_contract(source, "shopify_live_order"),
                              "shopify_live_order").first()["dq_reasons"]
    assert "varchar_overflow_shop_domain" in failed


def test_missing_customer_is_rejected(spark):
    row = source_row("shopify_customer_identity")
    row["customer_id"] = None
    source = spark.createDataFrame([tuple(row.values())],
                                   schema=", ".join(f"`{n}` string" for n in row))
    failed = glue.add_reasons(glue.to_contract(source, "shopify_customer_identity"),
                              "shopify_customer_identity").first()["dq_reasons"]
    assert "missing_or_invalid_customer_id" in failed


def test_duplicate_keys_fail_closed(spark):
    row = source_row("shopify_live_order")
    source = spark.createDataFrame([tuple(row.values()), tuple(row.values())],
                                   schema=", ".join(f"`{n}` string" for n in row))
    cleaned = glue.to_contract(source, "shopify_live_order")
    with pytest.raises(ValueError, match="Duplicate"):
        glue.ensure_unique(cleaned, glue.TABLE_KEYS["shopify_live_order"],
                           "shopify_live_order")


def test_glue_redshift_exact_column_contract():
    """Check the checked-in DDL without querying live AWS."""
    ddl = DDL.read_text()
    for table, schema in glue.CONTRACT.items():
        section = re.search(
            rf"CREATE TABLE IF NOT EXISTS silver_staging\.{table}\s*\((.*?)\)"
            rf"\s*DISTSTYLE", ddl, re.DOTALL
        )
        assert section is not None, table
        sql_columns = re.findall(
            r"([A-Za-z_][A-Za-z_0-9]*)\s+"
            r"(?:VARCHAR\(\d+\)|BIGINT|INT|"
            r"DECIMAL\(\d+,\d+\)|TIMESTAMPTZ|BOOLEAN)"
            r"(?=\s|,|$)",
            section.group(1),
        )
        assert sql_columns == [name for name, _ in schema], table


def test_publish_manifest_works_with_old_glue_botocore():
    """Old Glue Boto3 must not receive unsupported PutObject.IfNoneMatch."""
    from botocore.exceptions import ClientError

    class FakeS3:
        def __init__(self):
            self.created = None
            self.exists = False

        def head_object(self, **kwargs):
            if not self.exists:
                raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
            return {"ContentLength": len(self.created["Body"])}

        def put_object(self, **kwargs):
            if "IfNoneMatch" in kwargs:
                raise AssertionError("Unsupported bundled Glue botocore parameter")
            self.created = kwargs
            self.exists = True
            return {"ETag": "mock"}

    client = FakeS3()
    glue.publish_silver_manifest(
        client, "demo-bucket", "warehouse/shopify_snapshots/v1/batch/silver_manifest.json",
        {"counts": {"shopify_live_order": {"silver": 20}}},
    )
    assert client.created is not None
    assert client.created["ContentType"] == "application/json"
    assert b'"silver": 20' in client.created["Body"]
    with pytest.raises(FileExistsError):
        glue.publish_silver_manifest(client, "demo-bucket", "same-key", {})
