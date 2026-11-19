<#
.SYNOPSIS
  Reproduce the Phase 5 RDS migration 007 operations (preflight and apply).
.DESCRIPTION
  Defaults to metadata-only preview. -Execute runs a read-only ECS preflight.
  -Execute -Apply runs migration 007 and grants existing app-role privileges.
  NEVER launches the legacy DB bootstrap, which rotates app credentials.
.EXAMPLE
  .\scripts\aws\run_phase5_migration.ps1
  .\scripts\aws\run_phase5_migration.ps1 -Execute
  .\scripts\aws\run_phase5_migration.ps1 -Execute -Apply
#>
[CmdletBinding()]
param(
    [switch]$Execute,
    [switch]$Apply,
    [int]$MaxWaitMinutes = 30,
    [string]$ConfigPath = 'config/aws-demo.json'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($Apply -and -not $Execute) { throw 'Use -Execute -Apply together.' }
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
. (Join-Path $PSScriptRoot 'phase5_aws_common.ps1')
$config = Get-Content -LiteralPath (Join-Path $root $ConfigPath) -Raw | ConvertFrom-Json
$work = $config.phase5_migration
$common = Assert-Phase5Account $config
$image = '825414820081.dkr.ecr.eu-central-1.amazonaws.com/ecommerce-phase5-migration-demo@sha256:359e17670380993c89cff1868a00e44774f121b85929fdba1230ff20cf41b5f0'
$definition = Get-Phase5TaskDefinition -Family $work.task_family -Container $work.container -ImageDigest $image -Common $common
Write-Host "Cluster: $($config.cluster)"
Write-Host "Task: $($definition.taskDefinitionArn)"
Write-Host "Image: $image"
Write-Host "Private subnets: $($work.subnets -join ', ')"
Write-Host "Mode: $(if ($Apply) {'APPLY MIGRATION 007'} else {'READ-ONLY PREFLIGHT'})"
if (-not $Execute) { Write-Host 'PREVIEW ONLY. Add -Execute for preflight, or -Execute -Apply for migration.'; return }
$command = @()
if ($Apply) {
    $confirmation = Read-Host 'Type APPLY-MIGRATION-007 to authorize DB schema change'
    if ($confirmation -cne 'APPLY-MIGRATION-007') { throw 'Operator did not authorize migration.' }
    $command = @('--apply')
}
Invoke-Phase5Task -Config $config -Work $work -TaskDefinition $definition.taskDefinitionArn -Command $command -Common $common -MaxWaitMinutes $MaxWaitMinutes
