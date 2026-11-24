# Architecture and decisions

The platform has two clocks. Shopify can deliver an event whenever an order changes. Analysts see the change after a processing slot completes, a validated snapshot reaches Redshift and dbt builds the marts. The webhook response therefore establishes acceptance into a durable queue. The warehouse ledger and dbt marker establish analytical freshness.

## Deployment and routing

![Frankfurt VPC routing, database placement and S3 path](diagrams/exports/network.svg)

The project runs in a dedicated environment in Europe (Frankfurt), across two Availability Zones. The account identifier and physical subnet IDs are unnecessary for understanding the design. Each subnet tier has a distinct routing purpose.

| Tier | Placement and route | Consequence |
| --- | --- | --- |
| Public pair | Internet gateway default route. One on-demand public NAT gateway uses a reserved Elastic IP. The Tableau access host has its own security group. | Public IPv4 assignment is explicit. The access stack requests one for the host and uses an outbound SSM session. |
| Private application pair | ECS task ENIs, Glue JDBC connection and private Redshift workgroup connectivity. Both route tables reach S3 via its gateway endpoint. | S3 remains reachable between NAT windows. During a window both default routes point to the same available NAT. |
| Isolated database pair | Private RDS PostgreSQL subnet group with local VPC routing. | Projector and Glue connect through permitted security-group paths. Database traffic stays inside the VPC. |

API Gateway, queues, DynamoDB, S3 and the orchestration services are regional services. The network diagram places the clients and private connection points inside the VPC. The public webhook entrypoint stays available independently of application subnet internet routing.

## Architectural decisions

| Choice | Reason | Operational consequence |
| --- | --- | --- |
| Queue before projection | Shopify can deliver while RDS is stopped. | An acknowledgement alone cannot prove a report contains the order. Queue age and pending event state measure different backlogs. |
| S3 payload plus DynamoDB index | Retain replayable payloads and allow workers to find pending work without scanning the archive. | A checksum connects the index to object bytes. Recovery must preserve both the event identity and the database ledger. |
| PostgreSQL for operations, Redshift for analytics | Order updates require constraints and transactions. Reporting combines facts across sources and grains. | Glue establishes a quiescent extraction boundary. Full JDBC reads across tables do not provide a shared transaction snapshot. |
| Manifest-based warehouse publication | The loader can distinguish a complete batch from partially written objects. | The five Shopify staging tables refresh together. Historical synthetic tables have a separate load path. |
| Private dbt task and SQL churn model | SQL transformations, tests and scoring can run against the existing warehouse. | dbt refreshes features. Refitting the persisted classifier parameters is a separate SQL operation. |
| Shared, on-demand NAT | Concentrate public egress into predictable processing windows. | Creation takes time, one gateway serves both AZs and a premature release can strand a task. The controller checks both routes and outstanding work. |

![On-demand NAT control relationships](diagrams/exports/nat.svg)

## Ownership and evidence

Terraform owns the network foundation, operational service resources, Glue, Redshift and the NAT controller. The controller owns the ephemeral gateway and its application default routes. CloudFormation owns the coordinator, Silver loader, dbt orchestrator and Tableau access stacks and their child resources. The branch retains declarations for selected IAM, eventing, log and SSM imports. Declarations alone do not establish state ownership. Review the state before proposing adoption.

On November 24, the engineering record confirmed retirement of the legacy API/consumer/Spark/exporter repositories and associated legacy resources. The active projector security group and Glue connection are still needed. The record also found equivalent Glue program text after newline normalization and equivalent NAT controller logic despite a trailing comment in the deployed package. Historical deployment scripts and inbox/outbox migrations remain useful for reconstruction. The active ingress entry point is the Lambda-backed FastAPI handler.

The November 23 audit recorded a coordinator schedule that differed from its stored stack template. Verify the live schedule, Lambda package and stack before redeployment. The November 24 retirement record concerns infrastructure ownership, independently of that runtime check.

[Network definitions](../../infrastructure/terraform/networking.tf) · [NAT controller](../../scripts/aws/phase6_nat_lifecycle_lambda.py) · [Stack templates](../../infrastructure/cloudformation) · [Diagram sources and rendering](diagrams/README.md)
