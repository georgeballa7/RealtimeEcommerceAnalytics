"""S3 conditional-create ledger for irreversible Shopify lifecycle mutations."""
from __future__ import annotations

import json
from botocore.exceptions import ClientError


class S3LifecycleJournal:
    """Use S3 object versioning plus If-None-Match reservation, not local memory.

    Callers must serialize operations with the same key. Subsequent status writes
    are versioned S3 updates and SHOULD be serialized by a distributed lease.
    """
    def __init__(self, s3_client, bucket: str):
        if not bucket:
            raise ValueError("Bucket required")
        self.client = s3_client
        self.bucket = bucket
        self._etags = {}

    @staticmethod
    def path(key: str) -> str:
        import uuid
        if str(uuid.UUID(key)) != key:
            raise ValueError("Expected canonical operation UUID")
        return f"simulation/phase5/lifecycle/v1/{key}.json"

    def get(self, key: str) -> dict | None:
        try:
            obj = self.client.get_object(Bucket=self.bucket, Key=self.path(key))
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"NoSuchKey", "404", "NotFound"}:
                return None
            raise
        etag = obj.get("ETag")
        if not etag:
            raise RuntimeError("Cannot guard update: missing S3 ETag")
        self._etags[key] = etag
        return json.loads(obj["Body"].read())

    def put_new(self, key: str, record: dict):
        try:
            response = self.client.put_object(Bucket=self.bucket, Key=self.path(key),
                                   Body=json.dumps(record, sort_keys=True).encode("utf-8"),
                                   ContentType="application/json", IfNoneMatch="*")
            if response.get("ETag"):
                self._etags[key] = response["ETag"]
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"PreconditionFailed", "412", "ConditionalRequestConflict", "409"}:
                raise RuntimeError("Operation already reserved; reconcile before retry") from exc
            raise

    def put(self, key: str, record: dict):
        etag = self._etags.get(key)
        if not etag:
            raise RuntimeError("No prior S3 ETag; read or reserve before mutation")
        try:
            response = self.client.put_object(Bucket=self.bucket, Key=self.path(key),
                Body=json.dumps(record, sort_keys=True).encode("utf-8"),
                ContentType="application/json", IfMatch=etag)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {
                "PreconditionFailed", "412", "ConditionalRequestConflict", "409"
            }:
                raise RuntimeError("Concurrent operation update; reconcile before retry") from exc
            raise
        if not response.get("ETag"):
            raise RuntimeError("S3 status write succeeded but ETag was omitted; stop")
        self._etags[key] = response["ETag"]
