<#
.SYNOPSIS
    Apply the reviewed dbt-only Redshift SQL grants after ECS IAM auth-check.
.DESCRIPTION
    PREVIEW by default. Use -Execute ONLY after the first dbt AuthCheck task
    exits 0 and confirms the IAMR SQL identity exists. Uses the managed
    Redshift Serverless admin secret through Data API; no plaintext password.
    Executes the committed schema/grant migration as a single transaction.
    Never touches Silver data, RDS or Shopify.
.EXAMPLE
    .\scripts\aws\grant_phase7_dbt_access.ps1
.EXAMPLE
    .\scripts\aws\grant_phase7_dbt_access.ps1 -Execute
#>
[CmdletBinding()]
param([switch]$Execute)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Profile = 'ecommerce-admin'
$Region = 'eu-central-1'
$Account = '825414820081'
$Workgroup = 'ecommerce-analytics-demo'
$Database = 'analytics'
$Namespace = 'ecommerce-analytics-demo'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$File = Join-Path $Root 'analytics\redshift\prepare_dbt_warehouse_schemas.sql'
$Temp = $null

function Check-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed ($LASTEXITCODE)" }
}

$accountValue = (& aws sts get-caller-identity --profile $Profile --region $Region --query Account --output text | Out-String).Trim()
Check-Exit 'AWS identity'
if ($accountValue -ne $Account) { throw 'Incorrect AWS account' }
$source = [IO.File]::ReadAllText($File)
$source = [regex]::Replace($source, '(?m)^\s*--[^\r\n]*(?:\r?\n|$)', '')
$statements = @($source.Split(';') | ForEach-Object { $_.Trim() } | Where-Object { $_ })
if ($statements.Count -ne 8 -or $statements[0] -ne 'CREATE SCHEMA IF NOT EXISTS analytics_staging' -or
    $statements[1] -ne 'CREATE SCHEMA IF NOT EXISTS analytics_marts' -or
    $statements[2] -ne 'CREATE SCHEMA IF NOT EXISTS analytics' -or
    (($statements[3..7] -join ' ') -notmatch 'IAMR:ecommerce-phase6-dbt-task-demo')) {
    throw 'SQL migration differs from reviewed dbt-only contract'
}
Write-Host 'Reviewed Redshift dbt SQL migration: 3 CREATE SCHEMA + 5 GRANT statements.'
Write-Host 'Target: analytics / ecommerce-analytics-demo (eu-central-1).'
if (-not $Execute) {
    Write-Host 'PREVIEW ONLY. Run the dbt ECS AuthCheck first, then rerun with -Execute.'
    return
}
$secret = (& aws redshift-serverless get-namespace --namespace-name $Namespace --profile $Profile --region $Region --query namespace.adminPasswordSecretArn --output text | Out-String).Trim()
Check-Exit 'Redshift Serverless managed admin secret'
if ($secret -notmatch '^arn:aws:secretsmanager:eu-central-1:825414820081:secret:redshift!') {
    throw 'Unexpected managed Redshift admin secret'
}
$payload = @{
    WorkgroupName = $Workgroup
    Database = $Database
    SecretArn = $secret
    StatementName = 'phase7-dbt-iamr-schema-grants'
    Sqls = $statements
} | ConvertTo-Json -Depth 8 -Compress
$Temp = Join-Path ([IO.Path]::GetTempPath()) ('dbt-grants-' + [guid]::NewGuid().ToString('N') + '.json')
try {
    [IO.File]::WriteAllText($Temp,$payload,(New-Object Text.UTF8Encoding($false)))
    $uri = 'file://' + ($Temp -replace '\\', '/')
    $responseJson = & aws redshift-data batch-execute-statement --cli-input-json $uri --profile $Profile --region $Region --output json
    Check-Exit 'Submit dbt SQL migration'
    $execution = ($responseJson | Out-String | ConvertFrom-Json)
    if ($execution.Id -notmatch '^[0-9a-f-]{36}$') { throw 'Missing Redshift statement ID' }
    for ($attempt = 0; $attempt -lt 90; $attempt++) {
        $detailJson = & aws redshift-data describe-statement --id $execution.Id --profile $Profile --region $Region --output json
        Check-Exit 'Check Redshift grant migration'
        $detail = ($detailJson | Out-String | ConvertFrom-Json)
        if ($detail.Status -eq 'FINISHED') {
            Write-Host "dbt SQL schema/grant migration finished: $($execution.Id)"
            return
        }
        if ($detail.Status -in @('FAILED','ABORTED')) {
            throw "SQL grant migration $($detail.Status): $($detail.Error)"
        }
        Start-Sleep -Seconds 2
    }
    throw "SQL grant migration timeout. Check statement $($execution.Id) before retrying."
}
finally {
    if ($Temp) { Remove-Item -LiteralPath $Temp -Force -ErrorAction SilentlyContinue }
}
