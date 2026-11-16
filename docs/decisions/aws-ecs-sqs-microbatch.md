# ADR: Affordable event-driven analytics in AWS

**Decision (2026-11-16):** Retain local Kubernetes, Kafka, Spark and PostgreSQL as the development environment. Target AWS uses Amazon ECS/Fargate for stateless services; private RDS PostgreSQL for operational state and the transactional outbox; Amazon SQS with a dead-letter queue for asynchronous delivery; an idempotent ECS consumer persisting durable events to S3; scheduled PySpark microbatch tasks on Fargate; and Redshift Serverless/Power BI for later analytics.

The dedicated AWS VPC uses two public subnets with Internet Gateway and public HTTPS ALB, two private application subnets with AZ-local NAT gateways for outbound access, and two isolated RDS subnets without internet default routes. The database is never publicly accessible.

**Reasoning:** Avoid expensive always-on MSK and EMR streaming services for a small portfolio workload. SQS has different semantics from Kafka; events are replayed from durable S3 objects, not from a persistent queue log. Batch Spark in `local[*]` is not continuous distributed Spark Structured Streaming. Architecture can evolve to managed streaming services if requirements justify them.

**Deployment gate:** Do not activate ECS tasks until functional images, verified webhook HMAC, application database credentials, encryption/TLS, integration tests, idempotency and recovery checks are in place. No Terraform apply or production-readiness assertion is implied by committing infrastructure code.
