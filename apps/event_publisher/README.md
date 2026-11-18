# Transactional outbox publisher (PostgreSQL → SQS)

Implemented application logic: `src/ecommerce/application/use_cases/outbox_publisher.py`.

The Shopify webhook API persists an inbox and outbox row in one RDS transaction. The
publisher claims pending rows using `FOR UPDATE SKIP LOCKED` and a recoverable
lease, sends JSON to the existing **SQS standard queue**, and only then marks
the row published. Failure schedules a retry. An interrupted send may cause
duplicate delivery: downstream SQS-to-S3 consumers **must deduplicate** using
`(shop_domain, webhook_id)`. The publisher does not send directly to Kinesis.

Migrations, in order (not applied automatically):
- `infrastructure/sql/004_shopify_webhook_inbox_outbox.sql`
- `infrastructure/sql/005_shopify_webhook_outbox_leases.sql`

Offline unit tests are in `tests/unit/test_outbox_publisher.py`.

**Not yet deployed:** ECS publisher entrypoint, service/one-shot scheduling,
IAM/Secrets Manager wiring, integration tests against real PostgreSQL and SQS,
SQS-to-S3 consumer, and observability/alerts. This module alone does not process
live Shopify events. Before deployment, also verify webhook payload-size limits
against the SQS message limit and decide whether large payloads need S3 staging.
