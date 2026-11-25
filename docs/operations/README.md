# Operating the Frankfurt deployment

## Local setup

This repository contains **runnable local checks** and **AWS-connected operational workflows**. You do not need an AWS account, a Shopify store, or a running database to start reading the code and running the unit tests. Do not treat the commands used for a live deployment as a local quick-start.

### First 15 minutes

1. Read the [project overview](../../README.md) to understand the business outcome and technology choices.
2. Read the [pipeline walkthrough](../pipelines/README.md) to follow one signed Shopify delivery to the warehouse.
3. In the repository root, create a Python 3.12+ environment and install the relevant extras:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev,api,shopify,historical,postgres,aws]"
ruff check src tests
python -m pytest -q tests/unit
```

On macOS/Linux, use `source .venv/bin/activate`. **Do not run integration or end-to-end tests against an AWS account until their external-service requirements are understood.**

4. Follow one change through the code: the [HTTP webhook handler](../../src/ecommerce/interfaces/http/shopify_webhooks.py), [S3 landing worker](../../src/ecommerce/interfaces/workers/shopify_s3_lambda.py), [RDS projector](../../src/ecommerce/interfaces/workers/phase5_project_s3.py), and [their processing explanation](../pipelines/README.md#capture-and-projection).
5. For a transformation change, inspect [Glue Silver](../../scripts/glue/phase6_shopify_silver.py), [dbt models](../../analytics/dbt/models), [dbt tests](../../analytics/dbt/tests), and the [data-model guide](../data-model/README.md) **before** modifying warehouse definitions.

### Where a change belongs

| If you want to… | Look in… | Validate with… |
| --- | --- | --- |
| Change webhook validation or queue handling | `src/ecommerce/interfaces/http/`, `src/ecommerce/interfaces/workers/` | Python unit tests; relevant integration fixtures |
| Change synthetic order generation or schedules | `scripts/`, `infrastructure/terraform/`, `infrastructure/cloudformation/` | Idempotency tests; scheduled-job and operation-journal review |
| Change operational database constraints | `infrastructure/sql/` | Migration review, projector tests and rollback plan |
| Change Silver extraction or quality rules | `scripts/glue/`, [Silver contract](../data-model/README.md#live-shopify-silver-contract) | Schema/contract tests; batch-manifest checks |
| Add analytics metrics or marts | `analytics/dbt/models/`, `analytics/dbt/tests/` | dbt parsing and data tests in a suitably configured environment |
| Change private networking or the NAT controller | `infrastructure/terraform/`, `scripts/aws/` | Terraform validation, NAT unit tests, reviewed **state-aware plan** |
| Change Tableau access | `infrastructure/cloudformation/`, [connection steps](#connect-tableau-to-private-redshift) | Stack ownership review and read-only connection checks |

**Read-only versus mutating:** Listing AWS resources, reading status, and running unit tests are normally observation tasks. Creating Shopify orders, running backfills, applying Terraform plans, deploying CloudFormation stacks, refitting model parameters, or starting paid AWS compute are not. Use a development environment and explicit approval for changes that affect shared resources.

## Verify a slot with read-only access

Identify the Europe/Berlin processing date and slot. The generator uses morning/afternoon, while a downstream dbt marker uses morning/evening. Carry the batch ID through the following checks. Resource names are available in the configured deployment and CloudFormation outputs.

| Checkpoint | Evidence to inspect | What success establishes |
| --- | --- | --- |
| Generator and lifecycle | Generation marker for the slot. For the afternoon, both generation markers and the day's settled lifecycle marker. | Scheduled Shopify work reached its application completion boundary. |
| Landing and projection | Queue age, visible/in-flight counts, DLQ, pending event index, projector log and RDS event ledger. | Landed events were applied. An empty queue alone does not cover pending S3 objects. |
| Glue | Coordinator day/batch, Glue run status, Bronze/Silver manifests, source/accepted/quarantine counts. | The expected batch passed publication checks. |
| Redshift | Matching manifest key and digest in the load ledger, warehouse acknowledgement and verified table counts. | A committed snapshot reached staging. |
| dbt and Tableau | DynamoDB batch marker, ECS task exit code, dbt tests, then the workbook's reporting date and source filters. | Distinguishes a failed build from a stale extract or filtered dashboard. |
| End of slot | NAT release log, gateway state and both route tables. Check RDS, access host and active ECS/Glue workloads separately. | Verifies resource release independently of data freshness. |

The following AWS CLI examples read status. Set the AWS profile for the project and the indicated variables to actual deployed resource names in the current shell.

```sh
aws scheduler list-schedules --region eu-central-1 --query 'Schedules[].[Name,State]'
aws glue get-job-runs --region eu-central-1 --job-name "$GLUE_JOB_NAME" --max-results 3 --query 'JobRuns[].[Id,JobRunState,StartedOn]'
aws ecs describe-tasks --region eu-central-1 --cluster "$ECS_CLUSTER" --tasks "$TASK_ARN" --query 'tasks[].[lastStatus,stopCode,containers[].exitCode]'
aws ec2 describe-route-tables --region eu-central-1 --route-table-ids "$APP_ROUTE_A" "$APP_ROUTE_B" --query 'RouteTables[].Routes'
aws cloudwatch describe-alarms --region eu-central-1 --alarm-name-prefix ecommerce-demo --query 'MetricAlarms[].[AlarmName,StateValue]'
```

In an authorized read-only warehouse session, inspect load identity and the source split for the day under review. Querying Redshift can consume compute.

```sql
SELECT manifest_key, manifest_sha256
FROM silver_staging.warehouse_load_ledger
ORDER BY loaded_at DESC LIMIT 10;

SELECT order_date, source_system, is_synthetic, orders, gross_gbp, current_gbp
FROM analytics_marts.mart_daily_sales
ORDER BY order_date DESC, source_system LIMIT 20;
```

## Deployment and changes

The [bootstrap module](../../infrastructure/bootstrap) defines the versioned S3 state backend. The [main Terraform module](../../infrastructure/terraform/README.md) uses native state locking and deployment-specific feature flags and image digests. Backend configuration, plans, complete deployment variables and secrets belong outside version control. Use Secrets Manager references and task/Lambda execution roles. The Shopify signing secret verifies webhooks. The Shopify access token authorizes API operations.

Container Dockerfiles under [docker](../../docker) use the repository root as build context. The project builds role-specific images, pushes them to ECR and registers task definitions. The orchestrator validates the pinned dbt image digest before launch. A changed tag or a successful build alone cannot establish the deployed revision. Reconstruct effective settings from protected state before planning an update to an existing deployment. Review the saved plan for changes to active services and staged imports. The four [CloudFormation templates](../../infrastructure/cloudformation) have a separate deployment lifecycle.

[GitHub Actions](../../.github/workflows) runs Python lint, compilation and unit tests. Path-filtered checks cover PowerShell tooling, Glue contracts/Spark, Terraform offline validation and dbt parse. Passing CI establishes source-level checks. It does not deploy a stack or verify a processing slot.

**State-changing operations.** Publishing an image, registering a task, applying a saved plan, updating a stack, refitting the churn SQL model, replaying events and starting the Tableau tunnel can change remote state or incur compute charges. The Tableau helper can start the access EC2 instance. Use the [Tableau guide](#connect-tableau-to-private-redshift) to close the session when finished. Migrations and Shopify identity backfills have their own ledgers and are separate from routine reporting.

## Connect Tableau to private Redshift

Tableau Desktop uses a read-only Redshift identity through a Systems Manager tunnel. The workgroup is private. The CloudFormation template requests public IPv4 on the EC2 access host, which initiates the SSM connection. Its security group has no inbound SSH or RDP rule.

From the project root on Windows, with the AWS CLI, Session Manager plugin, Redshift connector/driver and configured AWS profile:

```powershell
.\scripts\tableau\get-tableau-credentials.ps1
.\scripts\tableau\start-tableau-tunnel.ps1
```

In Tableau, choose Amazon Redshift: host `localhost`, port `15439`, database `analytics`, temporary credentials returned by the script, TLS enabled. Pick the schema containing the tables needed for the analysis; the same connection has read-only access to `analytics_bi`, `analytics_marts`, `analytics_ml`, `analytics_staging`, `silver_staging`, `analytics` and `public`.

The Tableau database principal has a **24-hour idle session limit** and the regional SSM setting permits up to **60 minutes of idle tunneling**. Neither guarantees an indefinitely connected session. Temporary credentials expire for new authentication; an established session can remain usable. Reconnect if SSM closes the tunnel.

After the session, verify that the access instance has stopped. To stop it explicitly:

```powershell
.\scripts\tableau\stop-tableau-tunnel.ps1
```

Do not open Redshift to the public internet, commit temporary credentials, or leave the EC2 access host running unnecessarily. SQL grants and BI views are tracked in `scripts/redshift/`; Tableau workbook design and screenshots are managed separately in Notion.

See [AWS SSM port forwarding](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-sessions-start.html) and [Tableau Amazon Redshift connection](https://help.tableau.com/current/pro/desktop/en-us/examples_amazonredshift.htm).

## Failure diagnosis

| Symptom | First evidence | Recovery boundary |
| --- | --- | --- |
| Shopify receives 401, 403 or 503 | API logs, expected shop, signing-secret reference, SQS permissions and availability | Correct the cause and reconcile delivery identity. A 503 can be retried by the sender. |
| Queue drained, orders missing | DynamoDB pending index, controller error hold, RDS status, ECS stopped reason, object checksum | Resume the recorded event after identifying the failed boundary. Preserve the same shop and webhook ID. |
| Coordinator waiting | Exact marker payload, last projected event time, queue/in-flight and pending counts, worker and lease | Let the next reconciliation tick evaluate genuine completion. A fabricated marker can publish an incomplete snapshot. |
| Glue or loader fails | Glue run ID, quarantine, Silver manifest digest, loader logs and prior warehouse ledger | Resolve source/identity or COPY failure. Check the ledger before retry. A conflicting checksum requires investigation. |
| dbt launch uncertain | Batch marker, global lock, task ARN, task family, image digest and route readiness | Match the ECS task and its exit status before clearing any lock or launching again. |
| NAT still present | Controller reason, both slot records, recent activity, pending/running tasks, Glue runs and dbt state | A retained gateway can protect active work. Fix the blocking condition and observe the next controller tick. |

CloudWatch alarms cover DLQ messages, oldest queue age, RDS CPU and NAT controller errors. Check alarm actions and subscribed destinations in the deployment when diagnosing missing alerts. Treat queue metrics as approximate.

## Availability and cost controls

The NAT controller ticks every five minutes. Prewarming begins at 07:30 and 15:30. Normal release consideration starts at 08:30 or 16:30. Slot and warehouse/dbt evidence, a 20-minute activity quiet period and independent workload checks can extend these windows. A stale idle slot has an explicit release path. Release after staleness is not proof of successful data processing. The controller removes its application default routes before requesting gateway deletion and keeps the reserved Elastic IP. The S3 endpoint routes persist.

RDS startup latency is absorbed by the controller and pending index. The Glue lease protects JDBC extraction from competing stop decisions. On-demand tasks terminate after work. Redshift compute, persistent storage, snapshots, reserved public IPv4, CloudWatch retention and data transfer have separate cost drivers. A stopped database or deleted gateway alone cannot establish a zero-cost account.

For controlled shutdown, first stop future writers and schedules in a reviewed change, allow in-flight work to settle, then verify the last slot, ledger, dbt result and retained archives. Export required marts, model parameters, manifests and recovery evidence before retiring storage. Preserve the backend and its recovery path while reviewing Terraform plans and CloudFormation changes separately. Verify queue/DLQ contents, leases, active tasks, Glue jobs, NAT/routes and access sessions before teardown. Check subsequent billing and retained snapshots or addresses against the intended end state.
