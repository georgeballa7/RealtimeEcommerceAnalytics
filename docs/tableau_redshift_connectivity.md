# Tableau Desktop Free -> private Redshift Serverless

Status, 2026-11-21: AWS access stack deployed and Redshift TCP reachability verified. The **local Tableau Desktop application login remains untested**.

## Verified

- CloudFormation stack: ecommerce-tableau-ssm-access-demo, CREATE_COMPLETE
- On-demand t4g.micro EC2: i-04fa8d5f5592ca1e7; encrypted 8 GiB gp3 disk. Instance stopped after verification. EBS storage costs remain while stopped.
- Amazon Linux 2023 SSM agent version 3.3.5828.0 online in successful test. AWS Run Command resolved the private warehouse and reached TCP 5439.
- Access SG sg-01417e47617797488: NO ingress. Scoped outbound 443 to Systems Manager, 5439 to Redshift, VPC DNS. Redshift SG only allows this access SG and existing dbt SG.
- Redshift IAM role: ecommerce-tableau-bi-readonly-demo, trusted by existing george-admin IAM user; restricted to temporary credentials for analytics warehouse.
- Redshift database principal: IAMR:ecommerce-tableau-bi-readonly-demo, PASSWORD DISABLE, non-superuser; BI read access true, direct analytics_marts read access false.
- Seven deployed live SQL views in analytics_bi (4,382 current churn scores). These are **views**, not materialized copies.
- This CloudFormation stack is separate from Terraform ingestion state to avoid disrupting the scheduled batch. Source: infrastructure/cloudformation/tableau-ssm-on-demand-access.json.

## Install on Windows

Install Tableau Desktop Free Edition and Redshift ODBC driver 2.x, AWS CLI v2 and AWS Session Manager Plugin. Confirm the local AWS CLI profile ecommerce-admin works.

Run from project root in PowerShell:

Step 1. Obtain temporary, one-hour read-only database credentials:

    .\scripts\tableau\get-tableau-credentials.ps1

The command creates a local role-assumption AWS profile named tableau-ecommerce, prints the temporary database username, and copies a short-lived password to the Windows clipboard. No static DB password or AWS IAM key is created.

Step 2. In a second PowerShell window, create the private TCP tunnel:

    .\scripts\tableau\start-tableau-tunnel.ps1

The script starts the dedicated EC2 instance as needed, waits for SSM to come online, and keeps port forwarding open. It attempts to stop EC2 when the session exits. If necessary, stop it explicitly:

    .\scripts\tableau\stop-tableau-tunnel.ps1

## Tableau Desktop connection

Connect -> To a Server -> Amazon Redshift.

- Host: localhost
- Port: 15439
- Database: analytics
- Username: returned by credential script (expected IAMR:ecommerce-tableau-bi-readonly-demo)
- Password: paste from clipboard (expires after one hour)
- SSL: enabled
- Schema: analytics_bi

Start with view v_churn_model_metrics, then v_churn_risk_segments, v_churn_model_calibration, v_churn_monthly_labels, v_monthly_sales and v_customer_churn_scores.

An ODBC connection to localhost can conflict with strict TLS server-name verification; check the Tableau/ODBC driver settings during the first connection. Keep encrypted transport, and do not expose the private Redshift workgroup to the internet as a workaround.

This ChatGPT conversation cannot directly drive your locally installed Tableau Desktop GUI. I can maintain the warehouse, scripts, model and architecture; you operate or test the local Tableau connection, or use a product mode with supported computer control.

Security: do not publish pseudonymous customer-level data to Tableau Public. Use non-admin IAM credentials for BI. Do not leave the EC2 host running.

Docs:
- https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-sessions-start.html
- https://help.tableau.com/current/pro/desktop/en-us/examples_amazonredshift.htm
- https://docs.aws.amazon.com/redshift/latest/mgmt/serverless-iam-credentials.html
