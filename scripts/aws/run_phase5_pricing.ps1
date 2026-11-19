<#
.SYNOPSIS
  One ECS task reconciles all 3408 approved Shopify GBP variant prices to RDS.
.DESCRIPTION
  -Execute launches read-only full catalog comparison.
  -Execute -Apply requires interactive authorization for Shopify and RDS writes.
  No ten-variant cap, no permanent ECS service, no secrets in CLI or source.
.EXAMPLE
  .\scripts\aws\run_phase5_pricing.ps1
  .\scripts\aws\run_phase5_pricing.ps1 -Execute
  .\scripts\aws\run_phase5_pricing.ps1 -Execute -Apply
#>
[CmdletBinding()]
param(
    [switch]$Execute,
    [switch]$Apply,
    [int]$MaxWaitMinutes = 240,
    [string]$ConfigPath = 'config/aws-demo.json',
    [Parameter(Mandatory)][string]$ImageUri
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($Apply -and -not $Execute) { throw 'Use -Execute -Apply together.' }
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
. (Join-Path $PSScriptRoot 'phase5_aws_common.ps1')
$config = Get-Content -LiteralPath (Join-Path $root $ConfigPath) -Raw | ConvertFrom-Json
$work = $config.phase5_pricing
$common = Assert-Phase5Account $config
if (-not $ImageUri.StartsWith('825414820081.dkr.ecr.eu-central-1.amazonaws.com/ecommerce-phase5-pricing-demo@sha256:', [System.StringComparison]::Ordinal)) {
    throw 'Unexpected pricing ECR repository.'
}
$digest = $ImageUri.Split('@')[-1]
if ($digest -cnotmatch '^sha256:[0-9a-f]{64}$') {
    throw 'Expected full immutable ECR URI with sha256 digest.'
}
$image = $ImageUri
$definition = Get-Phase5TaskDefinition -Family $work.task_family -Container $work.container -ImageDigest $image -Common $common
$manifestSha = '7850c9f5faaea304b0d42c468a10179c8d44905f5ca82b7767e7497152fb2c06'
Write-Host "Cluster: $($config.cluster)"
Write-Host "Task: $($definition.taskDefinitionArn)"
Write-Host "Approved image: $image"
Write-Host "Expected catalog population: 3408"
Write-Host "Mode: $(if ($Apply) {'SHOPIFY + RDS WRITE'} else {'READ-ONLY COMPARE'})"
if (-not $Execute) { Write-Host 'PREVIEW ONLY. Add -Execute for read-only comparison.'; return }
$command = @('--limit','3408')
if ($Apply) {
    $confirmation = Read-Host 'Type AUTHORIZE-3408-GBP-PRICES to update Shopify and RDS'
    if ($confirmation -cne 'AUTHORIZE-3408-GBP-PRICES') { throw 'Write authorization not provided.' }
    $command = @('--apply','--limit','3408','--confirm-sha256',$manifestSha)
}
Invoke-Phase5Task -Config $config -Work $work -TaskDefinition $definition.taskDefinitionArn -Command $command -Common $common -MaxWaitMinutes $MaxWaitMinutes