"""AWS ECS consumer entrypoint; uses the ECS task role (no static AWS keys)."""
from __future__ import annotations

import logging
import os

import boto3

from ecommerce.application.use_cases.sqs_event_landing import SqsToS3LandingConsumer


def main() -> None:
    consumer = SqsToS3LandingConsumer(
        sqs_client=boto3.client("sqs"),
        s3_client=boto3.client("s3"),
        queue_url=os.environ["EVENT_QUEUE_URL"],
        bucket=os.environ["S3_LANDING_BUCKET"],
    )
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    logger = logging.getLogger(__name__)
    logger.info("SQS-to-S3 event consumer started")
    while True:
        counts = consumer.poll_once()
        if counts["received"]:
            logger.info(
                "Batch received=%d stored=%d replay=%d failed=%d",
                counts["received"],
                counts["created"],
                counts["duplicate"],
                counts["failed"],
            )


if __name__ == "__main__":
    main()
