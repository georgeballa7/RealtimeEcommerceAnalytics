# Shopify identity and ingestion implementation

The historical identity foundation reconciles **5,942** identifiable customers with Shopify and stores the verified mappings in private RDS. The historical product catalog uses an approval manifest (3,408 eligible stock codes); source records and reconciliation ledgers remain immutable or versioned in S3.

The deployed application additionally includes Shopify synthetic-order generation, signed webhooks, SQS/DLQ, S3 landing, DynamoDB coordination, private ECS projection and RDS, followed by Glue, Redshift and dbt. Refer to the [project README](README.md) for the current deployment.

**Operational safeguards:** Only approved Shopify writes, explicit source provenance, stable identity crosswalks, idempotent operation journals, leases, and read-back verification. An ambiguous remote create must be reconciled before retry. Do not repopulate customers or the catalog simply to retest deployment.

Historical run evidence and the completion status of individual catalog batches belong in the [Notion engineering record](https://app.notion.com/p/41f929a22c1581279669d9dca31d0e2d). This repository keeps reproducible code, contracts and tests; it is not the execution log.
