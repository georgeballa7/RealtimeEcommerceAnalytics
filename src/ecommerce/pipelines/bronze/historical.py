"""Immutable, auditable Bronze extraction of the UCI Online Retail II workbook."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pandas as pd

BRONZE_SCHEMA_VERSION = "online-retail-ii-bronze-v1"
EXPECTED_COLUMNS = (
    "Invoice", "StockCode", "Description", "Quantity", "InvoiceDate",
    "Price", "Customer ID", "Country",
)
TEXT_COLUMNS = {"Invoice": "string", "StockCode": "string", "Description": "string", "Country": "string"}


def file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def ingest_historical_bronze(source: Path, destination: Path) -> dict:
    """Export each worksheet unchanged in row count; fail rather than overwrite a prior run.

    Source workbook is never altered. Parquet stores Excel identifiers as strings,
    quantities as integer-like values, original timestamps without timezone conversion,
    and numeric Excel price values as received (Silver handles exact monetary decimals).
    """
    source = Path(source).resolve()
    destination = Path(destination).resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    if destination.exists():
        raise FileExistsError(f"Bronze export already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f".{destination.name}.tmp-{uuid4().hex}"
    staging.mkdir()
    try:
        source_hash = file_sha256(source)
        worksheets = pd.ExcelFile(source, engine="openpyxl")
        entries = []
        for index, sheet_name in enumerate(worksheets.sheet_names):
            frame = pd.read_excel(worksheets, sheet_name=sheet_name, dtype=TEXT_COLUMNS)
            if tuple(frame.columns) != EXPECTED_COLUMNS:
                raise ValueError(f"Unexpected source columns in {sheet_name!r}: {list(frame.columns)!r}")
            frame["source_sheet"] = sheet_name
            frame["source_row"] = pd.RangeIndex(len(frame))
            if frame["source_row"].duplicated().any():
                raise AssertionError("Source row identity is not unique")
            filename = f"worksheet-{index:02d}.parquet"
            output = staging / filename
            frame.to_parquet(output, engine="pyarrow", index=False)
            round_trip = pd.read_parquet(output, engine="pyarrow")
            if len(round_trip) != len(frame) or list(round_trip.columns) != list(frame.columns):
                raise AssertionError(f"Parquet verification failed for {sheet_name}")
            if not round_trip["source_row"].equals(frame["source_row"]):
                raise AssertionError(f"Source-row reconciliation failed for {sheet_name}")
            entries.append({
                "sheet_name": sheet_name,
                "file": filename,
                "rows": len(frame),
                "sha256": file_sha256(output),
            })
        manifest = {
            "schema_version": BRONZE_SCHEMA_VERSION,
            "source_filename": source.name,
            "source_sha256": source_hash,
            "extracted_at_utc": datetime.now(UTC).isoformat(),
            "source_columns": list(EXPECTED_COLUMNS),
            "provenance_columns": ["source_sheet", "source_row"],
            "worksheet_count": len(entries),
            "total_rows": sum(entry["rows"] for entry in entries),
            "worksheets": entries,
            "transformation_policy": "no deduplication, no filtering, no time zone conversion, no canonical ID mapping",
        }
        (staging / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        staging.rename(destination)
        return manifest
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
