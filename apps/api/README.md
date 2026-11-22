# Shopify webhook ingress

The deployed ingress is **API Gateway HTTP API → FastAPI Lambda** (not an ALB-hosted ECS API). The handler in `src/ecommerce/interfaces/http/shopify_webhooks_lambda.py` validates Shopify webhook signatures and queues accepted events for asynchronous processing.

SQS/DLQ, the landing Lambda, S3 archive and private ECS/RDS projector implement downstream processing. Keep business rules outside HTTP routes and never acknowledge an unverified payload as successfully processed. Infrastructure: `infrastructure/terraform/phase5_live_ingestion.tf`.
