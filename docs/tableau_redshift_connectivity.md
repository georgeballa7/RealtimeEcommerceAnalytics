# Tableau Desktop → private Redshift

Tableau Desktop uses a **read-only** Redshift identity via an AWS Systems Manager (SSM) tunnel through the dedicated EC2 access instance. Redshift itself stays private. The EC2 host is in a public subnet **without a public IP or inbound SSH/RDP rule**.

## Connect

From the project root on Windows, with the AWS CLI, Session Manager plugin, Redshift connector/driver and configured AWS profile:

```powershell
.\scripts\tableau\get-tableau-credentials.ps1
.\scripts\tableau\start-tableau-tunnel.ps1
```

In Tableau, choose Amazon Redshift: host `localhost`, port `15439`, database `analytics`, temporary credentials returned by the script, TLS enabled. Pick the schema containing the tables needed for the analysis; the same connection has read-only access to `analytics_bi`, `analytics_marts`, `analytics_ml`, `analytics_staging`, `silver_staging`, `analytics` and `public`.

The Tableau database principal has a **24-hour idle session limit** and the regional SSM setting permits up to **60 minutes of idle tunneling**. Neither guarantees an indefinitely connected session. Temporary credentials expire for new authentication; an established session can remain usable. Reconnect if SSM closes the tunnel.

When finished:

```powershell
.\scripts\tableau\stop-tableau-tunnel.ps1
```

Do not open Redshift to the public internet, commit temporary credentials, or leave the EC2 access host running unnecessarily. SQL grants and BI views are tracked in `scripts/redshift/`; Tableau workbook design and screenshots are managed separately in Notion.

See [AWS SSM port forwarding](https://docs.aws.amazon.com/systems-manager/latest/userguide/session-manager-working-with-sessions-start.html) and [Tableau Amazon Redshift connection](https://help.tableau.com/current/pro/desktop/en-us/examples_amazonredshift.htm).
