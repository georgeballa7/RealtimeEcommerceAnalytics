"""Unit tests for S3 publication without using AWS credentials or network."""

import io
import json
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from ecommerce.pipelines.bronze.s3 import upload_verified_bronze


class FakeClientError(Exception):
    def __init__(self, code: str):
        self.response = {"Error": {"Code": code}}


class MemoryBody(io.BytesIO):
    def iter_chunks(self, chunk_size: int):
        while chunk := self.read(chunk_size):
            yield chunk


class FakeS3:
    def __init__(self):
        self.objects = {}
        self.upload_order = []
        self.corrupt_key = None

    def get_bucket_location(self, **kwargs):
        return {"LocationConstraint": "eu-central-1"}

    def head_object(self, *, Bucket, Key):
        from botocore.exceptions import ClientError
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "missing"}}, "HeadObject")
        return {}

    def upload_file(self, path, bucket, key, ExtraArgs):
        content = Path(path).read_bytes()
        if self.corrupt_key == key:
            content += b"broken"
        self.objects[key] = content
        self.upload_order.append(key)

    def get_object(self, *, Bucket, Key):
        return {"Body": MemoryBody(self.objects[Key])}

    def put_object(self, *, Bucket, Key, Body, IfNoneMatch, ContentType):
        assert IfNoneMatch == "*"
        if Key in self.objects:
            raise AssertionError("Overwriting marker")
        self.objects[Key] = Body
        self.upload_order.append(Key)


def sample_book(path: Path) -> None:
    frame = pd.DataFrame({
        "Invoice": ["536365"], "StockCode": ["85123A"], "Description": ["Item"],
        "Quantity": [6], "InvoiceDate": pd.to_datetime(["2010-12-01 08:26"]),
        "Price": [2.55], "Customer ID": [17850], "Country": ["UK"],
    })
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        frame.to_excel(writer, sheet_name="Year 2009-2010", index=False)
        frame.to_excel(writer, sheet_name="Year 2010-2011", index=False)


def test_publish_manifest_last_and_no_overwrite(tmp_path: Path):
    pytest.importorskip("pyarrow")
    book = tmp_path / "source.xlsx"
    sample_book(book)
    client = FakeS3()
    session = SimpleNamespace(client=lambda _: client)
    result = upload_verified_bronze(book, bucket="test-bucket", prefix="bronze/v1",
                                    expected_full_dataset=False, session=session)
    assert result["total_rows"] == 2
    assert len(result["verified_s3_objects"]) == 3
    assert client.upload_order[-1] == "bronze/v1/manifest.json"
    assert client.objects["bronze/v1/source/source.xlsx"] == book.read_bytes()
    assert json.loads(client.objects["bronze/v1/manifest.json"]) == result
    with pytest.raises(FileExistsError):
        upload_verified_bronze(book, bucket="test-bucket", prefix="bronze/v1",
                               expected_full_dataset=False, session=session)


def test_rejects_mismatched_expected_source_counts(tmp_path: Path):
    pytest.importorskip("pyarrow")
    book = tmp_path / "source.xlsx"
    sample_book(book)
    client = FakeS3()
    with pytest.raises(ValueError, match="source counts"):
        upload_verified_bronze(book, bucket="test-bucket", prefix="bronze/v1",
                               expected_full_dataset=True,
                               session=SimpleNamespace(client=lambda _: client))
    assert client.objects == {}


def test_rejects_bad_prefix_before_any_upload(tmp_path: Path):
    book = tmp_path / "source.xlsx"
    sample_book(book)
    client = FakeS3()
    with pytest.raises(ValueError, match="normalized"):
        upload_verified_bronze(book, bucket="test-bucket", prefix="../oops",
                               expected_full_dataset=False,
                               session=SimpleNamespace(client=lambda _: client))
    assert client.objects == {}


def test_bad_remote_hash_blocks_manifest(tmp_path: Path):
    pytest.importorskip("pyarrow")
    book = tmp_path / "source.xlsx"
    sample_book(book)
    client = FakeS3()
    client.corrupt_key = "bronze/v1/data/worksheet-00.parquet"
    with pytest.raises(OSError, match="checksum mismatch"):
        upload_verified_bronze(book, bucket="test-bucket", prefix="bronze/v1",
                               expected_full_dataset=False,
                               session=SimpleNamespace(client=lambda _: client))
    assert "bronze/v1/manifest.json" not in client.objects


def test_manifest_is_commit_marker_without_parquet_dependency(tmp_path: Path, monkeypatch):
    """Exercise the upload path even where pyarrow is unavailable."""
    import ecommerce.pipelines.bronze.s3 as mod

    source = tmp_path / "source.xlsx"
    source.write_bytes(b"original workbook")

    def fake_export(src, destination):
        destination.mkdir(parents=True)
        (destination / "worksheet-00.parquet").write_bytes(b"parquet placeholder")
        return {
            "total_rows": 1_067_371,
            "source_sha256": sha256(source.read_bytes()).hexdigest(),
            "worksheets": [
                {"file": "worksheet-00.parquet", "rows": 525_461,
                 "sha256": sha256(b"parquet placeholder").hexdigest()},
                {"file": "worksheet-01.parquet", "rows": 541_910,
                 "sha256": sha256(b"parquet placeholder 2").hexdigest()},
            ],
        }

    def fake_both(src, destination):
        manifest = fake_export(src, destination)
        (destination / "worksheet-01.parquet").write_bytes(b"parquet placeholder 2")
        return manifest

    monkeypatch.setattr(mod, "ingest_historical_bronze", fake_both)
    client = FakeS3()
    result = mod.upload_verified_bronze(
        source, bucket="test-bucket", prefix="bronze/v1",
        session=SimpleNamespace(client=lambda _: client),
    )
    assert result["total_rows"] == 1_067_371
    assert client.upload_order[-1].endswith("manifest.json")
    assert len(result["verified_s3_objects"]) == 3
