# Architecture decisions

Application layers: `interfaces` → `application` → `domain`; `infrastructure` implements `application` ports. The domain must not import FastAPI, Shopify, SQL drivers, AWS or Databricks.

Data layers: historical/load & Shopify streaming → Delta Bronze → Silver → Gold. These Medallion layers are distinct from the application layers.

Runtime: Shopify → Kubernetes FastAPI → RDS PostgreSQL inbox/outbox → Kinesis → Databricks Structured Streaming → Delta tables → Databricks SQL → Power BI and churn scoring.

## Transaction boundaries

* RDS inbox and outbox records are written in one PostgreSQL transaction.
* Kinesis publishing occurs after commit with retries and deduplication.
* Delta Lake ACID transactions are per-table; no cross-platform/global ACID guarantee exists.
* Structured Streaming and deterministic keys must handle at-least-once events, late updates and replay.

The actual cutover timestamp is recorded only once live streaming is confirmed. Retrospective July–November events are labeled synthetic backfill, not live events. See the maintained Notion project plan for operational decisions.
