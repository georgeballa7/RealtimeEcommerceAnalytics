<#
Register Phase 5 Shopify live generator image through pinned Terraform plan.
Keeps the previously applied migration and pricing ECS definitions intact.
#>
[CmdletBinding()]
param([Parameter(Mandatory)][string]$ImageUri, [switch]$Execute)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$prefix = '825414820081.dkr.ecr.eu-central-1.amazonaws.com/ecommerce-phase5-generator-demo@'
if (-not $ImageUri.StartsWith($prefix, [System.StringComparison]::Ordinal) -or
    $ImageUri.Substring($prefix.Length) -cnotmatch '^sha256:[0-9a-f]{64}$') {
    throw 'Expected full immutable Phase 5 generator ECR image URI.'
}
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
. (Join-Path $PSScriptRoot 'phase5_aws_common.ps1')
$config = Get-Content -LiteralPath (Join-Path $root 'config/aws-demo.json') -Raw | ConvertFrom-Json
$common = Assert-Phase5Account $config
$migrationImage = '825414820081.dkr.ecr.eu-central-1.amazonaws.com/ecommerce-phase5-migration-demo@sha256:359e17670380993c89cff1868a00e44774f121b85929fdba1230ff20cf41b5f0'
$pricingImage = '825414820081.dkr.ecr.eu-central-1.amazonaws.com/ecommerce-phase5-pricing-demo@sha256:dde458ceadde92f0007de6c708924823a672be7af89aa8b0d6c5c8418a5c507f'
$plan = Join-Path ([IO.Path]::GetTempPath()) ('phase5-generator-' + [guid]::NewGuid() + '.tfplan')
Push-Location (Join-Path $root 'infrastructure/terraform')
try {
    & terraform plan "-var=phase5_generator_image=$ImageUri" "-var=phase5_migration_image=$migrationImage" "-var=phase5_pricing_image=$pricingImage" "-out=$plan"
    if ($LASTEXITCODE -ne 0) { throw 'Terraform planning failed.' }
    if (-not $Execute) { Write-Host 'PREVIEW ONLY. Add -Execute to register after reviewing plan.'; return }
    $confirm = Read-Host 'Type REGISTER-PHASE5-GENERATOR if plan adds only expected resources'
    if ($confirm -cne 'REGISTER-PHASE5-GENERATOR') { throw 'Registration not authorized.' }
    & terraform apply $plan
    if ($LASTEXITCODE -ne 0) { throw 'Terraform apply failed.' }
    & terraform output phase5_generator_task_definition
    if ($LASTEXITCODE -ne 0) { throw 'Failed to read ECS task definition output.' }
} finally {
    Pop-Location
    Remove-Item -LiteralPath $plan -Force -ErrorAction SilentlyContinue
}
