"""Tests for Bronze worksheet preservation and fail-safe writes."""

import json
from pathlib import Path

import pandas as pd
import pytest

from ecommerce.pipelines.historical_bronze import file_sha256, ingest_historical_bronze


@pytest.fixture
def workbook(tmp_path: Path) -> Path:
    source = tmp_path / "sample.xlsx"
    sheet = pd.DataFrame({
        "Invoice": ["536365", "C536366"],
        "StockCode": ["85123A", "POST"],
        "Description": ["Item", "Postage"],
        "Quantity": [6, -1],
        "InvoiceDate": pd.to_datetime(["2010-12-01 08:26", "2010-12-01 08:28"]),
        "Price": [2.55, 4.5],
        "Customer ID": [17850, None],
        "Country": ["United Kingdom", "United Kingdom"],
    })
    with pd.ExcelWriter(source, engine="openpyxl") as writer:
        sheet.to_excel(writer, sheet_name="Year 2009-2010", index=False)
        sheet.iloc[[0]].to_excel(writer, sheet_name="Year 2010-2011", index=False)
    return source


def test_bronze_retains_all_rows_and_provenance(workbook: Path, tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    before = file_sha256(workbook)
    dest = tmp_path / "bronze" / "v1"
    result = ingest_historical_bronze(workbook, dest)
    assert result["total_rows"] == 3
    assert result["worksheet_count"] == 2
    assert result["source_sha256"] == before == file_sha256(workbook)
    assert len(result["worksheets"]) == 2
    assert json.loads((dest / "manifest.json").read_text()) == result
    first = pd.read_parquet(dest / "worksheet-00.parquet")
    assert list(first["source_row"]) == [0, 1]
    assert list(first["source_sheet"]) == ["Year 2009-2010"] * 2
    assert list(first["Invoice"]) == ["536365", "C536366"]
    assert first["Customer ID"].iloc[0] == 17850
    assert pd.isna(first["Customer ID"].iloc[1])
    assert first["InvoiceDate"].dt.tz is None
    assert file_sha256(dest / "worksheet-00.parquet") == result["worksheets"][0]["sha256"]


def test_prevents_overwrite(workbook: Path, tmp_path: Path) -> None:
    dest = tmp_path / "existing"
    dest.mkdir()
    with pytest.raises(FileExistsError):
        ingest_historical_bronze(workbook, dest)


def test_rejects_unknown_schema(workbook: Path, tmp_path: Path) -> None:
    pytest.importorskip("pyarrow")
    source = pd.DataFrame({"Invoice": ["1"]})
    with pd.ExcelWriter(workbook, engine="openpyxl") as writer:
        source.to_excel(writer, sheet_name="unknown", index=False)
    dest = tmp_path / "bad"
    with pytest.raises(ValueError, match="Unexpected source columns"):
        ingest_historical_bronze(workbook, dest)
    assert not dest.exists()


def test_missing_source(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ingest_historical_bronze(tmp_path / "missing.xlsx", tmp_path / "bronze")
