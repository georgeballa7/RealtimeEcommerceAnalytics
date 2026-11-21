# Explicitly stop the on-demand Tableau EC2 access instance after work.
param([string]$Profile="ecommerce-admin",[string]$Region="eu-central-1")
$ErrorActionPreference="Stop"
$id=(& aws cloudformation describe-stacks --profile $Profile --region $Region --stack-name ecommerce-tableau-ssm-access-demo --query "Stacks[0].Outputs[?OutputKey=='AccessInstanceId'].OutputValue | [0]" --output text).Trim()
if ($LASTEXITCODE -ne 0 -or -not $id -or $id -eq "None") { throw "Could not find EC2 instance" }
& aws ec2 stop-instances --profile $Profile --region $Region --instance-ids $id --output table
if ($LASTEXITCODE -ne 0) { throw "Unable to stop EC2 instance" }
