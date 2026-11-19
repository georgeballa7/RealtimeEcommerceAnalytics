"""Process only indexed pending Shopify events; checkpoint after SQL commit.

A crash between PostgreSQL commit and DynamoDB update is safe: the RDS event
ledger makes the replay a duplicate and we can then checkpoint DynamoDB.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import boto3
import psycopg
from ecommerce.application.use_cases.live_order_projection import apply_envelope
from ecommerce.infrastructure.aws.event_index import claim_pending_events, mark_processed

LOG = logging.getLogger(__name__)


def project_pending(s3, table, connection, *, batch_size=25) -> dict:
    counts = {"processed": 0, "applied": 0, "duplicate": 0, "stale": 0}
    # DynamoDB GSI changes may lag behind writes. The controller performs a
    # subsequent pass before considering the backlog drained.
    while True:
        batch = claim_pending_events(table, limit=batch_size)
        if not batch:
            break
        progressed = False
        for item in batch:
            fresh = table.get_item(Key={"event_key": item["event_key"]}, ConsistentRead=True).get("Item")
            if not fresh or fresh.get("state") != "pending":
                continue  # lagging GSI result; already checkpointed
            obj = s3.get_object(Bucket=item["bucket"], Key=item["s3_key"])
            body = obj["Body"].read()
            checksum = hashlib.sha256(body).hexdigest()
            if checksum != item["checksum"] or obj.get("Metadata", {}).get("sha256") != checksum:
                raise RuntimeError("S3 webhook checksum mismatch")
            # apply_envelope owns a PostgreSQL transaction for the event.
            outcome = apply_envelope(connection, json.loads(body), item["s3_key"], checksum)
            mark_processed(table, item)
            progressed = True
            counts["processed"] += 1
            counts[outcome] += 1
        if not progressed:
            break  # GSI may still show processed entries; controller will revisit
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=25)
    args = parser.parse_args()
    required = ("DB_HOST", "DB_USER", "DB_PASSWORD", "DB_NAME", "DB_SSLROOTCERT",
                "PENDING_EVENTS_TABLE")
    for key in required:
        if not os.environ.get(key):
            parser.error("Missing " + key)
    logging.basicConfig(level=logging.INFO)
    if not Path(os.environ["DB_SSLROOTCERT"]).is_file():
        parser.error("RDS trust certificate is missing from the container")
    connection = psycopg.connect(host=os.environ["DB_HOST"], port=int(os.environ.get("DB_PORT", "5432")),
                                 dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"],
                                 password=os.environ["DB_PASSWORD"], sslmode="verify-full",
                                 sslrootcert=os.environ["DB_SSLROOTCERT"], connect_timeout=10)
    with connection:
        counts = project_pending(boto3.client("s3"),
                                 boto3.resource("dynamodb").Table(os.environ["PENDING_EVENTS_TABLE"]),
                                 connection, batch_size=args.batch_size)
    LOG.info("Phase 5 operational projection %s", json.dumps(counts, sort_keys=True))

if __name__ == "__main__":
    main()
