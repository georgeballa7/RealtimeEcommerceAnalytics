# Webhook API service

Deployment entry point for FastAPI. Responsible for verified Shopify webhook delivery and quick acknowledgements. HTTP routing belongs in `src/ecommerce/interfaces/http`; use cases belong in `src/ecommerce/application`. Do not put business rules in routes. **Not implemented yet.**
