# On-demand Windows -> SSM -> private Redshift. Requires AWS CLI v2 + Session Manager Plugin.
param([string]$Profile="ecommerce-admin",[string]$Region="eu-central-1",[int]$LocalPort=15439)
$ErrorActionPreference="Stop"
$hostName="ecommerce-analytics-demo.825414820081.eu-central-1.redshift-serverless.amazonaws.com"
if (-not (Get-Command aws -ErrorAction SilentlyContinue)) { throw "Install AWS CLI v2" }
if (-not (Get-Command session-manager-plugin -ErrorAction SilentlyContinue)) { throw "Install AWS Session Manager Plugin" }
$id=(& aws cloudformation describe-stacks --profile $Profile --region $Region --stack-name ecommerce-tableau-ssm-access-demo --query "Stacks[0].Outputs[?OutputKey=='AccessInstanceId'].OutputValue | [0]" --output text).Trim()
if ($LASTEXITCODE -ne 0 -or $id -eq "None" -or -not $id) { throw "Unable to resolve Tableau access EC2" }
$state=(& aws ec2 describe-instances --profile $Profile --region $Region --instance-ids $id --query "Reservations[0].Instances[0].State.Name" --output text).Trim()
if ($state -eq "stopping") {
  & aws ec2 wait instance-stopped --profile $Profile --region $Region --instance-ids $id
  if ($LASTEXITCODE -ne 0) { throw "EC2 did not stop" }
  $state="stopped"
}
if ($state -eq "stopped") {
  & aws ec2 start-instances --profile $Profile --region $Region --instance-ids $id --output text | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "EC2 start failed" }
} elseif ($state -notin @("running","pending")) { throw "Unexpected instance state: $state" }
try {
  & aws ec2 wait instance-running --profile $Profile --region $Region --instance-ids $id
  if ($LASTEXITCODE -ne 0) { throw "EC2 did not become running" }
  $online=$false
  for ($i=0; $i -lt 40; $i++) {
    $ping=(& aws ssm describe-instance-information --profile $Profile --region $Region --filters "Key=InstanceIds,Values=$id" --query "InstanceInformationList[0].PingStatus" --output text)
    if ($LASTEXITCODE -eq 0 -and $ping -eq "Online") { $online=$true; break }
    Start-Sleep -Seconds 6
  }
  if (-not $online) { throw "SSM agent never became Online" }
  Write-Host "Tunnel: localhost:$LocalPort -> private Redshift:5439. Keep this PowerShell window open."
  $ssmParameters="host=$hostName,portNumber=5439,localPortNumber=$LocalPort"
  & aws ssm start-session --profile $Profile --region $Region --target $id --document-name AWS-StartPortForwardingSessionToRemoteHost --parameters $ssmParameters
  if ($LASTEXITCODE -ne 0) { throw "SSM tunnel could not be established" }
} finally {
  Write-Host "Stopping temporary Tableau EC2 instance..."
  & aws ec2 stop-instances --profile $Profile --region $Region --instance-ids $id --output text | Out-Null
  if ($LASTEXITCODE -ne 0) { Write-Warning "Stop EC2 manually or use stop-tableau-tunnel.ps1" }
}
