# Real-Time E-commerce Analytics on AWS

**An event-driven data engineering project that turns Shopify transactions into reliable sales reporting and customer-retention insights.**

A sales event is only useful for analytics when it is **captured, processed once, reconciled, and reflected in the reporting tables**. This project builds that path end to end: simulated Shopify orders enter through secure webhooks, are processed into PostgreSQL, and then flow through AWS Glue, Amazon Redshift Serverless, and dbt into Tableau-ready data models.

The platform is designed around an important constraint: **the private application database and outbound networking do not need to run continuously.** Incoming events can be accepted while RDS is stopped; scheduled processing and reconciliation carry them through to analytics later.

![End-to-end system architecture showing Shopify ingestion, AWS operational processing, warehouse, and Tableau](docs/architecture/diagrams/exports/overview.svg)

## At a glance

| Question | Answer |
| --- | --- |
| **What is this?** | A portfolio-scale, AWS-hosted e-commerce data platform covering event ingestion, transactional processing, analytics engineering, and churn modeling. |
| **Why build it?** | To demonstrate how unreliable or out-of-order event delivery becomes trustworthy, auditable business data. |
| **What data does it use?** | A Shopify development store with controlled synthetic transactions and a separately reconciled historical retail dataset. **Not live customer commerce.** |
| **What can an analyst explore?** | Orders, sales trends, refunds, cancellations, customer segments, and a baseline estimate of 30-day inactivity risk. |
| **What makes it interesting technically?** | Replay-safe processing, distinct operational and analytical stores, batch publication checks, private networking, and an on-demand NAT lifecycle. |
| **Is it production-ready?** | No. It is an implemented learning and portfolio environment with tested safeguards; production use would need additional availability, security, operational, and data-governance work. |

## See what was built

| Stage | Technology | Purpose |
| --- | --- | --- |
| **Generate** | Shopify development store, scheduled ECS tasks | Produce controlled orders and lifecycle events with traceable source identities. |
| **Receive** | API Gateway, FastAPI on Lambda, SQS, S3 | Authenticate webhook deliveries and keep them durable even if PostgreSQL is offline. |
| **Process** | DynamoDB, ECS/Fargate, Amazon RDS PostgreSQL | Reconcile pending events, apply order/refund changes, and prevent duplicate database effects. |
| **Prepare** | AWS Glue, S3 Bronze/Silver | Extract and validate a complete processing snapshot before publication. |
| **Analyze** | Redshift Serverless, dbt Core | Load verified batches and build dimensional models, metrics, and customer features. |
| **Explore** | Redshift SQL, Tableau | Query sales marts and churn-scoring views through controlled private access. |

**Example business question:** *Did yesterday's simulated orders reach the dashboard?* The answer is not merely “the webhook returned success.” The platform checks the source processing marker, event ledger, validated snapshot manifest, Redshift load acknowledgement, and dbt task outcome. [Follow that evidence path](docs/operations/README.md).

## Choose your route

### I am reviewing this project

Start with the [architecture guide](docs/architecture/README.md) and [pipeline walkthrough](docs/pipelines/README.md). They explain the design decisions, processing guarantees, networking, and what happens when a service fails.

Then visit [data modeling and lineage](docs/data-model/README.md) for the operational ERDs, analytical fact/dimension tables, and churn model. The churn analysis includes limitations: its score estimates a defined inactivity outcome on synthetic/historical data; it is not a validated commercial prediction product.

### I want to run or extend the code

Start with [the developer path in the operations guide](docs/operations/README.md#developer-start-here), which separates **safe local checks** from **AWS-changing operations**.

Useful entry points:

| Task | Start here |
| --- | --- |
| Follow an event end to end | [Pipeline walkthrough](docs/pipelines/README.md) |
| Find the webhook handler and projection code | [Capture and projection](docs/pipelines/README.md#capture-and-projection) |
| Understand schemas and data quality | [Data models](docs/data-model/README.md) and [Silver contract](docs/phase6_shopify_silver_contract.md) |
| Work with dbt models and SQL | [dbt models](analytics/dbt/models) and [Redshift SQL](analytics/redshift) |
| Inspect Terraform and AWS ownership | [Infrastructure reference](infrastructure/terraform/README.md) and [architecture decisions](docs/architecture/README.md) |
| Investigate a pipeline failure | [Operations and recovery](docs/operations/README.md#failure-diagnosis) |

## Run the local checks

These commands **do not require AWS access** and **do not create Shopify transactions**. From the repository root with Python 3.12 or newer:

```powershell
python -m venv .venv
# Windows PowerShell:
.\.venv\Scripts\Activate.ps1

python -m pip install -e ".[dev,api,shopify,historical,postgres,aws]"
ruff check src tests
python -m pytest -q tests/unit
```

On macOS/Linux, activate with `source .venv/bin/activate`. The unit tests use fixtures and mocks; integration and end-to-end tests have different requirements. See [operations](docs/operations/README.md) before invoking cloud-connected scripts.

For the original historical workbook workflow, use the [historical scripts guide](scripts/README.md). For the live platform, do **not** use sample Terraform variables against a deployed account: infrastructure plans require the complete, verified deployment settings.

## How fresh are the results?

The documentation records a successful Glue → Redshift → dbt processing checkpoint on **21 November 2026** and the retirement of **23 obsolete AWS resources on 24 November 2026**. These are dated observations, not a claim that a particular reporting date is current.

To verify a new processing day, use the [read-only slot verification checklist](docs/operations/README.md#verify-a-slot-with-read-only-access).

## Documentation map

- [Architecture and network](docs/architecture/README.md) — high-level system, routing, security and design trade-offs.
- [Pipelines and recovery boundaries](docs/pipelines/README.md) — generation, webhook capture, projection, Glue, Redshift, dbt and churn.
- [Database models and ERDs](docs/data-model/README.md) — tables, keys, grains and data lineage.
- [Operations and developer setup](docs/operations/README.md) — how to inspect, test, troubleshoot and deploy safely.
- [Tableau connectivity](docs/tableau_redshift_connectivity.md) — private reporting access.
- [Churn model analysis](docs/churn_sql_model_v1.md) — methodology, evaluation and limitations.
- [Architecture diagrams and editable sources](docs/architecture/diagrams/README.md) — overview, network, ingestion, warehouse and NAT.
- [Documentation validation and evidence](docs/validation.md) — what was checked, and what was not.
