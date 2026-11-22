# End-to-end checks

Acceptance scope: Shopify development-store generation → signed API Gateway/Lambda webhook → SQS/DLQ → S3 landing and private RDS projection → Glue Silver → Redshift → dbt tests. Check duplicate delivery, restart/replay, reconciliation and NAT release.

A test name or fixture does not establish a successful AWS run. Execute live checks only with explicitly authorized credentials and budget safeguards.
