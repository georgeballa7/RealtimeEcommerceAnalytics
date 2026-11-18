# SQS → S3 event landing consumer

Application: `src/ecommerce/application/use_cases/sqs_event_landing.py`
ECS entrypoint: `src/ecommerce/interfaces/workers/sqs_to_s3.py`
Container: `Dockerfile.consumer`

The consumer uses the existing `EVENT_QUEUE_URL` and `S3_LANDING_BUCKET`
variables in `infrastructure/terraform/worker.tf`. It long-polls the existing
encrypted SQS standard queue and stores each Shopify webhook envelope as a
conditional-create JSON object under `events/shopify/v1/` in the existing
versioned, encrypted landing bucket.

**Idempotency:** object key = SHA-256(shop_domain + NUL + webhook_id). A repeat
delivery can be deleted from SQS only after the existing S3 object's SHA-256
metadata matches the canonical event bytes. Conflicting payloads are never
silently overwritten or acknowledged. Original event/customer payloads are
not emitted to logs.

**Failure semantics:** SQS messages are deleted only after S3 storage succeeds.
On S3 errors, invalid envelope, hash conflict, or failed SQS delete, the
message will become visible again and may reach the existing SQS DLQ after
its configured redrive count. Processing is **at least once**, not exactly once.
The S3 record retains origin and Shopify webhook ID. These event files are
an input to the scheduled PySpark microbatch, not raw Shopify historical sales.

**Validated:** 10 focused offline unit tests; no AWS or Shopify writes.
**Not yet completed:** build and push container, review Terraform plan,
deploy ECS consumer, test real SQS/S3 behavior, set alarms, build downstream
PySpark transformation. The original customer-sync ECS task is independent.
