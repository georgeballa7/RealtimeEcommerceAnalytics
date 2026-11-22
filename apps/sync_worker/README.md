# Shopify identity synchronization

The identity-sync and reconciliation workers use Shopify Admin GraphQL, verified canonical IDs, private RDS mapping/lease tables, and guarded ECS/Fargate execution. They are **on-demand bootstrap/reconciliation tools**, not a continuously running generic sync service.

Use the approved identity/catalog manifests and the scripts in `scripts/aws/`. Completed customer population must not be repeated merely for a deployment check.
