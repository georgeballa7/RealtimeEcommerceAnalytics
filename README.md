# Real-Time E-commerce Analytics

AWS-based Shopify order ingestion, operational processing, dimensional analytics, and customer-retention modeling. Shopify transactions in this demonstration are **synthetic**; historical Online Retail II data and generated events have distinct provenance.

## System activity diagrams

The diagrams below separate **operational event processing** from **warehouse transformation**. Solid arrows represent the primary processing path; decision nodes show validation and retry handling.

### 1. Shopify event processing

```mermaid
flowchart TB
    start((Start)) --> schedule["EventBridge schedule"]
    schedule --> generate["ECS/Fargate: generate synthetic orders"]
    generate --> shopify["Shopify development store"]
    shopify --> webhook["Webhook to API Gateway"]
    webhook --> validate{"Signature and payload valid?"}
    validate -- No --> reject["Reject request"]
    reject --> stop1((End))
    validate -- Yes --> intake["FastAPI Lambda: accept event"]
    intake --> queue["Amazon SQS"]
    queue --> landing["Landing Lambda: persist event"]
    landing --> archive["Amazon S3: event archive"]
    landing --> index["DynamoDB: event index and coordination"]
    archive --> projector["Private ECS: project event"]
    index -. "Coordinate" .-> projector
    projector --> persist{"Processed successfully?"}
    persist -- Yes --> rds["Private RDS PostgreSQL: operational state"]
    persist -- No --> retry["Retry / redrive policy"]
    retry --> dlq["SQS dead-letter queue"]
    rds --> finish((End))

    classDef decision fill:#fff2cf,stroke:#b7791f,color:#202020
    classDef store fill:#e7f0ff,stroke:#3b6cb7,color:#202020
    class validate,persist decision
    class queue,archive,index,rds,dlq store
```

### 2. Operational data to analytics

```mermaid
flowchart TB
    begin((Start)) --> rds["RDS PostgreSQL: operational records"]
    rds --> glue["AWS Glue: extract, validate and reconcile"]
    glue --> checks{"Quality checks pass?"}
    checks -- No --> investigate["Log and investigate exceptions"]
    investigate --> endfail((Stop))
    checks -- Yes --> silver["Amazon S3: Bronze and Silver"]
    silver --> load["Load Redshift silver_staging"]
    load --> dbt["dbt Core on ECS/Fargate"]
    dbt --> tests{"Model tests pass?"}
    tests -- No --> fix["Investigate failed transformations"]
    fix --> endfail
    tests -- Yes --> marts["Redshift: analytics marts"]
    marts --> features["Churn features and analytical views"]
    marts --> tableau["Tableau Desktop: read-only reporting"]
    features --> done((End))
    tableau --> done

    classDef decision fill:#fff2cf,stroke:#b7791f,color:#202020
    classDef store fill:#e7f0ff,stroke:#3b6cb7,color:#202020
    class checks,tests decision
    class rds,silver,load,marts store
```

- **Ingestion and operations:** Shopify event generation, webhook validation, durable event storage, queue-based processing, and private PostgreSQL operational state.
- **Analytics:** Glue extraction and validation, Redshift staging, dbt dimensions/facts and churn-feature marts with data-quality tests.
- **Reporting:** Tableau connects to Redshift analytics schemas through controlled read-only access.
- **Infrastructure:** Terraform and CloudFormation manage AWS resources in `eu-central-1`. Deployment uses private application and isolated database networking where required.


The churn model and transactions are a demonstration, **not** a production decision system or real-customer revenue pipeline.

## Repository guide

| Location | Purpose |
| --- | --- |
| `src/ecommerce/`, `apps/` | Domain, application, interfaces, and AWS workloads |
| `scripts/`, `scripts/aws/`, `scripts/glue/` | Ingestion, deployment and orchestration |
| `infrastructure/terraform/` | Core AWS infrastructure as code |
| `infrastructure/cloudformation/` | Separately managed infrastructure stacks |
| `analytics/dbt/`, `analytics/redshift/`, `scripts/redshift/` | Warehouse transformations, SQL and access controls |
| `tests/` | Unit, integration and infrastructure checks |

## Local development

```powershell
python -m pip install -e ".[dev,historical,aws]"
ruff check src apps tests
python -m pytest -q tests/unit
```

Read [Terraform safeguards](infrastructure/terraform/README.md) before changing AWS resources. Historical ingestion entrypoints are documented in [scripts/README.md](scripts/README.md).
