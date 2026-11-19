<#
.SYNOPSIS
  Plan or apply the Terraform registration of Phase 5 ECS pricing task.
.DESCRIPTION
  Always uses immutable approved ECR digest. Does not launch ECS tasks.
  The same image variable must be supplied in subsequent Terraform plans.
#>
[CmdletBinding()]
param(
    [switch]$Execute,
    [Parameter(Mandatory)][string]$ImageUri
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$config = Get-Content -LiteralPath (Join-Path $root 'config/aws-demo.json') -Raw | ConvertFrom-Json
. (Join-Path $PSScriptRoot 'phase5_aws_common.ps1')
$common = Assert-Phase5Account $config
if (-not $ImageUri.StartsWith('825414820081.dkr.ecr.eu-central-1.amazonaws.com/ecommerce-phase5-pricing-demo@sha256:', [System.StringComparison]::Ordinal)) {
    throw 'Unexpected pricing ECR repository.'
}
$digest = $ImageUri.Split('@')[-1]
if ($digest -cnotmatch '^sha256:[0-9a-f]{64}$') {
    throw 'Expected full immutable ECR URI with sha256 digest.'
}
$image = $ImageUri
$migrationImage = '825414820081.dkr.ecr.eu-central-1.amazonaws.com/ecommerce-phase5-migration-demo@sha256:359e17670380993c89cff1868a00e44774f121b85929fdba1230ff20cf41b5f0'
$planPath = Join-Path ([IO.Path]::GetTempPath()) ('phase5-pricing-' + [guid]::NewGuid() + '.tfplan')
Push-Location (Join-Path $root 'infrastructure/terraform')
try {
    & terraform plan "-var=phase5_pricing_image=$image" "-var=phase5_migration_image=$migrationImage" "-out=$planPath"
    if ($LASTEXITCODE -ne 0) { throw 'Terraform plan failed. No infrastructure changed.' }
    Write-Host 'Review the proposed Terraform resources above.'
    Write-Host 'Expected: add pricing task definition and its log group; no changes or destroys.'
    Write-Host 'Migration image variable is explicitly retained to prevent task removal.'
    if (-not $Execute) { Write-Host 'PREVIEW ONLY. Use -Execute after reviewing the plan.'; return }
    $confirmation = Read-Host 'Type REGISTER-PHASE5-PRICING to apply the saved plan'
    if ($confirmation -cne 'REGISTER-PHASE5-PRICING') { throw 'Registration not authorized.' }
    & terraform apply $planPath
    if ($LASTEXITCODE -ne 0) { throw 'Terraform apply failed; inspect state before retrying.' }
    & terraform output phase5_pricing_task_definition
    if ($LASTEXITCODE -ne 0) { throw 'Terraform output lookup failed.' }
} finally {
    Pop-Location
    Remove-Item -LiteralPath $planPath -Force -ErrorAction SilentlyContinue
}