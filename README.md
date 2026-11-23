# Real-Time E-commerce Analytics

AWS-based Shopify order ingestion, operational processing, dimensional analytics, and customer-retention modeling. Shopify transactions in this demonstration are **synthetic**; historical Online Retail II data and generated events have distinct provenance.

## End-to-end pipeline

```mermaid
flowchart LR
    A["EventBridge<br/>ECS order generator"] --> B["Shopify<br/>development store"]
    B -->|Webhooks| C["API Gateway<br/>FastAPI Lambda"]
    C --> D["SQS + landing Lambda<br/>S3 event archive"]
    D --> E["ECS projector<br/>RDS PostgreSQL"]
    E --> F["AWS Glue<br/>S3 Bronze / Silver"]
    F --> G["Redshift Serverless<br/>dbt analytics marts"]
    G --> H["Tableau<br/>dashboards"]

    D -.-> I["DynamoDB<br/>coordination"]
    C -.-> J["SQS DLQ<br/>failed events"]

    classDef process fill:#eef4ff,stroke:#5278b8,color:#1e293b,stroke-width:1px
    classDef storage fill:#e9f6f1,stroke:#358067,color:#1e293b,stroke-width:1px
    classDef exception fill:#fff4e5,stroke:#ba8740,color:#1e293b,stroke-width:1px
    class A,B,C,F,H process
    class D,E,G,I storage
    class J exception
```

- **Ingestion and operations:** Shopify event generation, webhook validation, durable event storage, queue-based processing, and private PostgreSQL operational state.
- **Analytics:** Glue extraction and validation, Redshift staging, dbt dimensions/facts and churn-feature marts with data-quality tests.
- **Reporting:** Tableau connects to Redshift analytics schemas through controlled read-only access.
- **Infrastructure:** Terraform and CloudFormation manage AWS resources in `eu-central-1`. Deployment uses private application and isolated database networking where required.

The churn model and transactions are a demonstration, **not** a production decision system or real-customer revenue pipeline.

## Repository guide

| Location | Purpose |
| --- | --- |
| `src/ecommerce/` | Domain, application, and interfaces |
| `scripts/`, `scripts/aws/`, `scripts/glue/` | Ingestion, deployment and orchestration |
| `docker/` | Container build definitions (root build context) |
| `infrastructure/terraform/` | Core AWS infrastructure as code |
| `infrastructure/cloudformation/` | Separately managed infrastructure stacks |
| `analytics/dbt/`, `analytics/redshift/`, `scripts/redshift/` | Warehouse transformations, SQL and access controls |
| `tests/` | Unit, integration and infrastructure checks |

## Local development

```powershell
python -m pip install -e ".[dev,historical,aws]"
ruff check src tests
python -m pytest -q tests/unit
```

Read [Terraform safeguards](infrastructure/terraform/README.md) before changing AWS resources. Historical ingestion entrypoints are documented in [scripts/README.md](scripts/README.md).
