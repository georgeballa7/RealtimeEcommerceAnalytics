# Tableau ↔ private Redshift Serverless: connection checklist

## Current verified status (2026-11-21)

**Database setup completed in AWS:**
- Redshift database `analytics`, schema `analytics_bi`
- Seven curated SELECT views of sales and churn metrics
- Role `tableau_analytics_reader`; scoped SELECT on all existing and future `analytics_bi` tables and views
- Read query against `analytics_bi.v_churn_model_metrics` succeeds (ROC-AUC 0.6398, 7,524 validation examples)
- No Tableau identity mapped to the role yet
- Warehouse private; inbound Redshift TCP 5439 currently from dbt ECS SG only
- No SSM bastion, VPN, Tableau Cloud private connection, or Bridge network path deployed
- No Redshift public exposure or new billable networking provisioned

## Recommended local Desktop connection

1. Install [Tableau Desktop Free Edition](https://www.tableau.com/pricing) (live DB connections supported, local-only publishing) and [Amazon Redshift ODBC 2.x driver](https://help.tableau.com/current/pro/desktop/en-us/examples_amazonredshift.htm).
2. Define a secure on-demand private VPC connectivity solution; for a low-frequency local Windows session use a small EC2 Session Manager port-forwarding bastion. The required Terraform resource and SG + IAM design must be cost-reviewed and deployed **before** attempting live login.
3. Create a non-admin Redshift login or IAM-federated principal; assign ONLY `tableau_analytics_reader`. Never reuse Redshift admin secret for BI.
4. Use native Redshift connection in Tableau Desktop, SSL required, database `analytics`, select schema `analytics_bi`. Test `SELECT * FROM analytics_bi.v_churn_model_metrics;`.
5. Build sheets from `v_churn_risk_segments`, `v_churn_monthly_labels`, `v_churn_model_calibration`, `v_churn_risk_by_recency`, `v_monthly_sales` and `v_customer_churn_scores`.
6. Save workbook locally with live connection. Shut down temporary SSM instance after analytics work. Live Tableau needs tunnel running; screenshots and extracts do not.
7. Future publishing: Tableau Cloud Creator/Professional + private-connected Bridge or Outbound Private Connect. Requires separate site and licensing; do not enable always-on cost without review.

Network example **after** bastion deployment:

```powershell
aws ssm start-session `
  --profile ecommerce-admin `
  --region eu-central-1 `
  --target <bastion-instance-id> `
  --document-name AWS-StartPortForwardingSessionToRemoteHost `
  --parameters '{"host":["ecommerce-analytics-demo.825414820081.eu-central-1.redshift-serverless.amazonaws.com"],"portNumber":["5439"],"localPortNumber":["15439"]}'
```

While session stays open, native Tableau hostname `localhost`, port `15439` with appropriate TLS/ODBC settings. IAM for `StartSession` must be narrowly scoped; use no inbound SSH. Real implementation and SSL test remain to be done.

References:
- [Tableau Redshift connector](https://help.tableau.com/current/pro/desktop/en-us/examples_amazonredshift.htm)
- [Tableau Desktop Free vs Professional](https://help.tableau.com/current/pro/desktop/en-us/desktop_comparison.htm)
- [Private Redshift via Tableau Bridge](https://help.tableau.com/current/online/en-us/to_bridge_vpc.htm)
- [AWS Redshift GRANT RBAC](https://docs.aws.amazon.com/redshift/latest/dg/r_GRANT.html)
