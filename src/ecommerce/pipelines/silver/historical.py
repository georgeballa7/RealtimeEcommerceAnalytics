"""Bronze-to-Silver for Online Retail II; source-faithful and auditable.

Silver customer-linked records exclude missing/invalid customer identifiers.
Rejected records and cross-sheet copies remain traceable in separate outputs.
"""
from __future__ import annotations

import io
import json
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pandas as pd

from ecommerce.domain.identifiers import (
    UCI_SOURCE,
    canonical_id,
    historical_customer_id,
    historical_order_id,
    historical_order_item_id,
    historical_product_id,
)

SILVER_VERSION = "online-retail-ii-silver-v1"
BRONZE_VERSION = "online-retail-ii-bronze-v1"
DEFAULT_BRONZE_PREFIX = "historical/online_retail_ii/bronze/v1"
DEFAULT_SILVER_PREFIX = "historical/online_retail_ii/silver/v1"
NON_MERCHANDISE = {
    "POST": "shipping_or_fee", "C2": "shipping_or_fee",
    "DOT": "shipping_or_fee", "BANK CHARGES": "shipping_or_fee",
    "D": "discount", "M": "manual_adjustment",
}


def _sha256_bytes(payload: bytes) -> str:
    return sha256(payload).hexdigest()


def _read_object(s3: Any, bucket: str, key: str) -> bytes:
    body = s3.get_object(Bucket=bucket, Key=key)["Body"]
    try:
        return body.read()
    finally:
        body.close()


def load_verified_bronze(s3: Any, bucket: str, prefix: str) -> tuple[pd.DataFrame, dict]:
    """Read only published Bronze datasets and verify every input checksum."""
    prefix = prefix.strip("/")
    manifest = json.loads(_read_object(s3, bucket, f"{prefix}/manifest.json"))
    if manifest["schema_version"] != BRONZE_VERSION:
        raise ValueError("Unexpected Bronze schema version")
    # Check the uploaded source workbook as well, not only derived Parquet files.
    raw_workbook = _read_object(s3, bucket, manifest["source_s3_key"])
    if _sha256_bytes(raw_workbook) != manifest["source_sha256"]:
        raise ValueError("Bronze source checksum mismatch")
    frames = []
    for entry in manifest["worksheets"]:
        key = f"{prefix}/data/{entry['file']}"
        payload = _read_object(s3, bucket, key)
        if _sha256_bytes(payload) != entry["sha256"]:
            raise ValueError(f"Bronze checksum mismatch: {key}")
        df = pd.read_parquet(io.BytesIO(payload), engine="pyarrow")
        if len(df) != entry["rows"]:
            raise ValueError(f"Bronze row count mismatch: {key}")
        if not df["source_sheet"].eq(entry["sheet_name"]).all():
            raise ValueError("Bronze sheet provenance mismatch")
        frames.append(df)
    if not frames:
        raise ValueError("No Bronze worksheets")
    combined = pd.concat(frames, ignore_index=True)
    if len(combined) != manifest["total_rows"]:
        raise ValueError("Bronze total row count mismatch")
    return combined, manifest


def _source_customer_ids(series: pd.Series) -> pd.Series:
    """Map integer-like Excel customer IDs to strings; reject other values."""
    values = pd.to_numeric(series, errors="coerce")
    valid = values.notna() & values.ge(0) & values.eq(values.round())
    return values.where(valid).astype("Int64").astype("string")


def transform_bronze(frame: pd.DataFrame) -> tuple[dict[str, pd.DataFrame], dict[str, int]]:
    """Build stable, customer-linked Silver entities plus auditable exclusions."""
    needed = {
        "Invoice", "StockCode", "Description", "Quantity", "InvoiceDate",
        "Price", "Customer ID", "Country", "source_sheet", "source_row",
    }
    if needed - set(frame):
        raise ValueError(f"Missing Bronze columns: {sorted(needed - set(frame))}")
    d = frame.copy()
    d["source_record_key"] = d["source_sheet"].astype(str) + ":" + d["source_row"].astype(str)
    if not d["source_record_key"].is_unique:
        raise ValueError("Duplicate Bronze source row keys")

    # Mirror notebook 02's occurrence-based cross-sheet duplicate policy.
    source_cols = ["Invoice", "StockCode", "Description", "Quantity", "InvoiceDate", "Price", "Customer ID", "Country"]
    d["fingerprint"] = pd.util.hash_pandas_object(d[source_cols], index=False)
    d["occurrence"] = d.groupby(["source_sheet", "fingerprint"], sort=False).cumcount()
    match = d.groupby(["fingerprint", "occurrence"], sort=False)["source_sheet"].transform("nunique").gt(1)
    # Identical rows in two worksheets must appear as a matched pair, never >2 copies.
    match_size = d.groupby(["fingerprint", "occurrence"], sort=False)["source_sheet"].transform("size")
    if (match & match_size.ne(2)).any():
        raise ValueError("Unexpected multi-sheet duplicate structure")
    # The second worksheet is authoritative for overlapping rows.
    sheet_order = list(dict.fromkeys(d["source_sheet"].tolist()))
    if len(sheet_order) != 2:
        raise ValueError("Expected two source worksheets")
    d["removed_cross_sheet_copy"] = match & d["source_sheet"].ne(sheet_order[-1])
    duplicates = d.loc[d["removed_cross_sheet_copy"], ["source_record_key", "source_sheet", "source_row"]].copy()
    duplicates["reason"] = "duplicate_cross_sheet"
    retained = d.loc[~d["removed_cross_sheet_copy"]].copy()

    retained["source_customer_id"] = _source_customer_ids(retained["Customer ID"])
    missing = retained["source_customer_id"].isna()
    quarantine = retained.loc[missing, ["source_record_key", "source_sheet", "source_row", "Invoice", "Customer ID"]].copy()
    quarantine["reason"] = "missing_or_invalid_customer_id"
    eligible = retained.loc[~missing].copy()

    eligible["source_order_id"] = eligible["Invoice"].astype("string").str.strip()
    eligible["source_stock_code"] = eligible["StockCode"].astype("string").str.strip()
    if eligible["source_order_id"].isna().any() or eligible["source_order_id"].eq("").any():
        raise ValueError("Customer-linked invoice IDs must be present")
    eligible["customer_id"] = eligible["source_customer_id"].map(historical_customer_id)
    eligible["order_id"] = eligible["source_order_id"].map(historical_order_id)
    eligible["order_item_id"] = eligible["source_record_key"].map(historical_order_item_id)
    eligible["product_id"] = eligible["source_stock_code"].map(
        lambda value: historical_product_id(str(value)) if pd.notna(value) and str(value) else None
    )
    eligible["order_timestamp"] = pd.to_datetime(eligible["InvoiceDate"], errors="coerce")
    eligible["stock_normalized"] = eligible["source_stock_code"].str.upper()
    eligible["item_class"] = "requires_review"
    merchandise = eligible["stock_normalized"].str.fullmatch(r"[0-9]{5}[A-Z]{0,2}", na=False)
    eligible.loc[merchandise, "item_class"] = "merchandise_candidate"
    for code, item_class in NON_MERCHANDISE.items():
        eligible.loc[eligible["stock_normalized"].eq(code), "item_class"] = item_class
    eligible["is_cancellation"] = eligible["source_order_id"].str.upper().str.startswith("C", na=False)
    eligible["is_return_or_credit"] = eligible["is_cancellation"] | eligible["Quantity"].lt(0)
    eligible["is_positive_sale"] = (
        eligible["Quantity"].gt(0) & eligible["Price"].gt(0) & ~eligible["is_cancellation"]
        & eligible["item_class"].eq("merchandise_candidate")
    )
    # Decimal conversion from string avoids storing financial values as floats.
    eligible["unit_price"] = eligible["Price"].map(lambda v: Decimal(str(v)) if pd.notna(v) else None)
    eligible["line_amount"] = [
        int(q) * p if pd.notna(q) and p is not None else None
        for q, p in zip(eligible["Quantity"], eligible["unit_price"])
    ]

    customers = (eligible[["customer_id", "source_customer_id", "order_timestamp"]]
                 .groupby(["customer_id", "source_customer_id"], as_index=False, dropna=False)
                 .agg(first_seen_at=("order_timestamp", "min")))
    customers["source_system"] = UCI_SOURCE
    customers["shopify_customer_gid"] = None
    customers["origin"] = "historical"

    # Source stock code is the canonical product grain. Variant mappings are a
    # separate Shopify sync concern, never invented from historical stock codes.
    products = eligible.loc[eligible["product_id"].notna()].sort_values("source_record_key")
    products = products.drop_duplicates("product_id")[
        ["product_id", "source_stock_code", "Description", "item_class"]
    ].rename(columns={"Description": "description"}).copy()
    products["source_system"] = UCI_SOURCE
    products["shopify_product_gid"] = None
    variants = products[["product_id", "source_stock_code"]].copy()
    variants["variant_id"] = variants["source_stock_code"].map(
        lambda code: canonical_id("variant", UCI_SOURCE, str(code))
    )
    variants["sku"] = variants["source_stock_code"]
    variants["shopify_variant_gid"] = None
    variants["source_system"] = UCI_SOURCE

    inconsistent_customers = eligible.groupby("order_id")["customer_id"].nunique().gt(1)
    if inconsistent_customers.any():
        raise ValueError("Historical invoice maps to multiple customers")
    orders = (eligible.sort_values("order_timestamp")
              .drop_duplicates("order_id")
              [["order_id", "source_order_id", "customer_id", "order_timestamp", "Country"]]
              .rename(columns={"Country": "country"}).copy())
    orders["source_system"] = UCI_SOURCE
    orders["order_number"] = orders["source_order_id"]
    orders["currency"] = None  # Original dataset has no explicit currency column; do not invent it.
    orders["origin"] = "historical"
    orders["shopify_order_gid"] = None

    items = eligible[[
        "order_item_id", "order_id", "product_id", "source_record_key", "source_order_id",
        "source_stock_code", "Quantity", "unit_price", "line_amount", "item_class",
        "is_cancellation", "is_return_or_credit", "is_positive_sale", "order_timestamp",
    ]].rename(columns={"Quantity": "quantity"}).copy()
    adjustments = items.loc[~items["is_positive_sale"]].copy()
    result = {
        "customers": customers, "products": products, "product_variants": variants, "orders": orders,
        "order_items": items, "returns_adjustments": adjustments,
        "quarantine": quarantine, "cross_sheet_duplicates": duplicates,
    }
    if len(eligible) + len(quarantine) + len(duplicates) != len(d):
        raise AssertionError("Bronze-to-Silver row reconciliation failed")
    if not items["order_item_id"].is_unique or not orders["order_id"].is_unique:
        raise AssertionError("Canonical primary keys must be unique")
    if not items["order_id"].isin(orders["order_id"]).all():
        raise AssertionError("Orphan order items")
    if not orders["customer_id"].isin(customers["customer_id"]).all():
        raise AssertionError("Orphan customers")
    stats = {
        "bronze_lines": len(d), "cross_sheet_duplicate_lines": len(duplicates),
        "quarantined_lines": len(quarantine), "customer_linked_lines": len(eligible),
        "customers": len(customers), "orders": len(orders),
        "positive_merchandise_lines": int(items["is_positive_sale"].sum()),
    }
    return result, stats


def publish_silver(
    s3: Any, bucket: str, bronze_prefix: str = DEFAULT_BRONZE_PREFIX,
    silver_prefix: str = DEFAULT_SILVER_PREFIX,
) -> dict:
    """Read verified S3 Bronze; write checked Silver Parquet; commit manifest last."""
    from botocore.exceptions import ClientError

    prefix = silver_prefix.strip("/")
    if not prefix or "//" in prefix or any(x in {".", ".."} for x in prefix.split("/")):
        raise ValueError("Invalid Silver prefix")

    def exists(key: str) -> bool:
        try:
            s3.head_object(Bucket=bucket, Key=key)
            return True
        except ClientError as error:
            if error.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    if exists(f"{prefix}/manifest.json"):
        raise FileExistsError("Silver version is already published")
    bronze, bronze_manifest = load_verified_bronze(s3, bucket, bronze_prefix)
    tables, counts = transform_bronze(bronze)
    if bronze_manifest["total_rows"] == 1_067_371 and counts["customers"] != 5_942:
        raise ValueError(
            f"Customer-population reconciliation failed: {counts['customers']} != 5,942"
        )
    with TemporaryDirectory(prefix="ecommerce-silver-") as temporary:
        files = []
        for name, df in tables.items():
            local = Path(temporary) / f"{name}.parquet"
            df.to_parquet(local, index=False, engine="pyarrow")
            key = f"{prefix}/{name}/part-00000.parquet"
            if exists(key):
                raise FileExistsError(f"Silver object already exists: {key}")
            files.append((local, key, len(df)))
        output = []
        for path, key, rows in files:
            digest = _sha256_bytes(path.read_bytes())
            s3.upload_file(str(path), bucket, key, ExtraArgs={"Metadata": {"sha256": digest}})
            if _sha256_bytes(_read_object(s3, bucket, key)) != digest:
                raise IOError(f"Silver remote checksum mismatch: {key}")
            output.append({"key": key, "rows": rows, "sha256": digest})
        manifest = {
            "schema_version": SILVER_VERSION,
            "source_bronze_manifest_key": f"{bronze_prefix.strip('/')}/manifest.json",
            "source_bronze_sha256": bronze_manifest["source_sha256"],
            "created_at_utc": datetime.now(UTC).isoformat(),
            "reconciliation": counts, "files": output,
        }
        marker = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
        s3.put_object(Bucket=bucket, Key=f"{prefix}/manifest.json", Body=marker,
                      ContentType="application/json", IfNoneMatch="*")
        if _read_object(s3, bucket, f"{prefix}/manifest.json") != marker:
            raise IOError("Silver manifest verification failed")
        return manifest
