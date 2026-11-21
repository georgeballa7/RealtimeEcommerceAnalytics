# Copy one-hour temporary Redshift read-only password to clipboard, no static secrets.
param([string]$SourceProfile="ecommerce-admin",[string]$Region="eu-central-1",[string]$ReadOnlyProfile="tableau-ecommerce")
$ErrorActionPreference="Stop"
$role=(& aws cloudformation describe-stacks --profile $SourceProfile --region $Region --stack-name ecommerce-tableau-ssm-access-demo --query "Stacks[0].Outputs[?OutputKey=='ReadOnlyIamRoleArn'].OutputValue | [0]" --output text).Trim()
if ($LASTEXITCODE -ne 0 -or -not $role -or $role -eq "None") { throw "Read-only IAM role not found" }
& aws configure set "profile.$ReadOnlyProfile.role_arn" $role
& aws configure set "profile.$ReadOnlyProfile.source_profile" $SourceProfile
& aws configure set "profile.$ReadOnlyProfile.region" $Region
$json=(& aws redshift-serverless get-credentials --profile $ReadOnlyProfile --region $Region --workgroup-name ecommerce-analytics-demo --db-name analytics --duration-seconds 3600 --output json) | Out-String
if ($LASTEXITCODE -ne 0 -or -not $json) { throw "GetCredentials failed" }
$auth=$json | ConvertFrom-Json
if (-not $auth.dbUser -or -not $auth.dbPassword) { throw "Invalid Redshift credentials response" }
Set-Clipboard -Value $auth.dbPassword
Write-Host "Tableau username: $($auth.dbUser)"
Write-Host "Temporary password copied to clipboard; paste into Tableau Password field."
Write-Host "Expires at: $($auth.expiration)"
Write-Host "Server: localhost | Port: 15439 | Database: analytics | Schema: analytics_bi"
Write-Host "Clear the clipboard when finished."
$auth.dbPassword=$null
