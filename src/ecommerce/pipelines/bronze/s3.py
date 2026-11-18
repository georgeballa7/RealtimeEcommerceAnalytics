"""Publish a verified Bronze export to the project S3 landing bucket.

Publication is deliberately append-only. A manifest is written only after
all source and Parquet objects have passed remote SHA-256 verification.
"""

from __future__ import annotations

import json
import re
import subprocess
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from ecommerce.pipelines.bronze.historical import ingest_historical_bronze

DEFAULT_PREFIX = "historical/online_retail_ii/bronze/v1"
EXPECTED_FULL_ROWS = 1_067_371
EXPECTED_FULL_SHEET_ROWS = (525_461, 541_910)


def terraform_landing_bucket(terraform_dir: Path) -> str:
    """Read the deployed bucket name from Terraform state, not from a guessed name."""
    run = subprocess.run(
        ["terraform", f"-chdir={Path(terraform_dir).resolve()}", "output", "-raw", "landing_bucket_name"],
        check=True,
        capture_output=True,
        text=True,
    )
    name = run.stdout.strip()
    if not name or not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", name):
        raise ValueError("Terraform did not return a valid landing bucket name")
    return name


def _object_exists(s3: Any, bucket: str, key: str) -> bool:
    from botocore.exceptions import ClientError

    try:
        s3.head_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        # Permission failures must never be interpreted as missing objects.
        if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise


def _remote_sha256(s3: Any, bucket: str, key: str) -> str:
    response = s3.get_object(Bucket=bucket, Key=key)
    digest = sha256()
    body = response["Body"]
    try:
        for chunk in body.iter_chunks(chunk_size=1024 * 1024):
            if chunk:
                digest.update(chunk)
    finally:
        body.close()
    return digest.hexdigest()


def _assert_bucket_region(s3: Any, bucket: str, expected_region: str) -> None:
    response = s3.get_bucket_location(Bucket=bucket)
    actual = response.get("LocationConstraint") or "us-east-1"
    if actual != expected_region:
        raise ValueError(f"Bucket {bucket} belongs to {actual}, expected {expected_region}")


def upload_verified_bronze(
    source: Path,
    *,
    bucket: str,
    prefix: str = DEFAULT_PREFIX,
    aws_profile: str = "ecommerce-admin",
    region: str = "eu-central-1",
    expected_full_dataset: bool = True,
    session: Any = None,
) -> dict[str, Any]:
    """Convert to temporary Parquet and commit a verified, immutable S3 dataset.

    The source and Parquet files are uploaded to the final prefix and checked
    by streaming remote contents back to the hash function. The manifest is
    created last using a conditional S3 PUT. A failed upload leaves the prefix
    uncommitted; it is never presented as successfully published.
    """
    import boto3

    prefix = prefix.strip("/")
    if not prefix or "//" in prefix or any(part in {".", ".."} for part in prefix.split("/")):
        raise ValueError("Use a nonempty, normalized, versioned S3 prefix")
    if not bucket.strip():
        raise ValueError("S3 bucket name is required")
    session = session or boto3.Session(profile_name=aws_profile, region_name=region)
    s3 = session.client("s3")
    _assert_bucket_region(s3, bucket, region)

    manifest_key = f"{prefix}/manifest.json"
    if _object_exists(s3, bucket, manifest_key):
        raise FileExistsError(f"Bronze S3 manifest already exists: s3://{bucket}/{manifest_key}")

    with TemporaryDirectory(prefix="ecommerce-bronze-") as tmp:
        local_export = Path(tmp) / "bronze"
        manifest = ingest_historical_bronze(Path(source), local_export)
        if expected_full_dataset:
            counts = tuple(item["rows"] for item in manifest["worksheets"])
            if manifest["total_rows"] != EXPECTED_FULL_ROWS or counts != EXPECTED_FULL_SHEET_ROWS:
                raise ValueError(f"Unexpected Online Retail II source counts: {counts}")
        objects = [(Path(source).resolve(), f"{prefix}/source/{Path(source).name}", manifest["source_sha256"])]
        for item in manifest["worksheets"]:
            objects.append((local_export / item["file"], f"{prefix}/data/{item['file']}", item["sha256"]))

        # Do not overwrite partial artifacts either. A failed run requires a
        # new versioned prefix or deliberate operator reconciliation.
        collisions = [key for _, key, _ in objects if _object_exists(s3, bucket, key)]
        if collisions:
            raise FileExistsError("S3 prefix already contains data objects: " + ", ".join(collisions))

        for local_file, key, expected_hash in objects:
            s3.upload_file(str(local_file), bucket, key, ExtraArgs={"Metadata": {"sha256": expected_hash}})
            actual_hash = _remote_sha256(s3, bucket, key)
            if actual_hash != expected_hash:
                raise IOError(f"S3 checksum mismatch: s3://{bucket}/{key}")

        published = {
            **manifest,
            "s3_bucket": bucket,
            "s3_prefix": prefix,
            "published_at_utc": datetime.now(UTC).isoformat(),
            "source_s3_key": objects[0][1],
            "verified_s3_objects": [
                {"key": key, "sha256": digest} for _, key, digest in objects
            ],
        }
        # S3 conditional write protects the commit marker from replacement.
        payload = (json.dumps(published, indent=2) + "\n").encode("utf-8")
        s3.put_object(
            Bucket=bucket, Key=manifest_key, Body=payload,
            ContentType="application/json", IfNoneMatch="*",
        )
        observed = s3.get_object(Bucket=bucket, Key=manifest_key)["Body"].read()
        if observed != payload:
            raise IOError("Uploaded manifest did not verify")
        return published
