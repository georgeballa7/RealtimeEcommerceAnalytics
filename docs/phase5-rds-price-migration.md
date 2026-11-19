# Phase 5 RDS price-state migration (007)

Purpose: create **only** `shopify_variant_price_state`, using the existing
RDS-managed master secret and the application role `ecommerce_app`.
This is a one-off Fargate task, not a service. No credential rotation.

## Why not run the old bootstrap image?

`ecommerce-db-bootstrap-demo` executes `bootstrap_database_user.py`.
That script rotates and republishes the application password and only bundles
SQL 001–003. Do NOT rerun it for migration 007.

The older migrations may have been applied outside `schema_migrations`. The
Phase 5 migration runner explicitly checks that catalog identity tables exist,
applies ONLY SQL 007, and records version 007 and its checksum. It does not
attempt to replay migrations 001–006. Future adoption of the full migration
runner should reconcile any earlier untracked migrations separately.

## Safe workflow (operator approval at each step)

1. Pull the feature branch and run:
   `python -m pytest -q tests/unit/test_phase5_price_migration.py`.
2. From `infrastructure/terraform`, review a `terraform plan` with the
   existing workspace/variables (the new ECR repository exists independent of
   the image variable). Apply only after reviewing unrelated drift.
3. Build image from repo root:
   `docker build -f Dockerfile.phase5-migration -t ecommerce-phase5-migration-demo:phase5 .`
4. Push to the new ECR repository with a unique immutable tag, using the
   existing AWS profile and secure ECR login (do not put passwords in CLI args).
   Resolve its image digest from ECR, then set
   `phase5_migration_image=<repository>@sha256:<digest>` in the Terraform
   variables. Review plan before registering the task definition.
5. Run a single ECS Fargate task in existing application subnets with the
   existing ECS security group, `assignPublicIp=DISABLED`. No command
   override means **preflight only**, no DDL.
6. Inspect the exit code and CloudWatch log group
   `/ecs/ecommerce-demo/phase5-migration`.
7. After reviewing the preflight output, run a one-off task with
   container override command `["--apply"]`. This authorizes SQL 007 and
   its application role grants in one PostgreSQL transaction.
8. Check logs show `applied=True` and `applied` (or
   `already_applied` on an idempotent rerun).
9. Stop: do not run the Shopify price updater until the first 10-variant
   read-only comparison has been reviewed.

## Security properties

- The master password is injected by ECS from the same RDS-managed secret
  already used by bootstrap. No new secret is created or read into Terraform.
- The dedicated ECS execution role can read only that one secret.
- The migration container does not have an application AWS task role.
- The price-state table grants the existing application role
  `SELECT, INSERT, UPDATE`, not DDL or DELETE.
- Read-only preflight takes an advisory lock and SELECTs metadata only.
- The apply transaction fails closed if 007 has a mismatched checksum or
  if the price table exists without a matching migration record.

Never use the bootstrap task for this step; that task rotates credentials.
