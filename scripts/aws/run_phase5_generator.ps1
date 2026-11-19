<#
Launch a single traceable Shopify order generator task.
Default is preview; -Execute -Apply creates synthetic orders in Shopify.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ImageUri,
    [Parameter(Mandatory)][string]$RunId,
    [ValidateRange(1,100)][int]$Count = 45,
    [switch]$Execute,
    [switch]$Apply,
    [int]$MaxWaitMinutes = 180
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
if ($Apply -and -not $Execute) { throw 'Use -Execute -Apply together.' }
if ($RunId -cnotmatch '^[A-Za-z0-9][A-Za-z0-9_-]{2,63}$') { throw 'Invalid RunId.' }
$prefix = '825414820081.dkr.ecr.eu-central-1.amazonaws.com/ecommerce-phase5-generator-demo@'
if (-not $ImageUri.StartsWith($prefix, [System.StringComparison]::Ordinal) -or
    $ImageUri.Substring($prefix.Length) -cnotmatch '^sha256:[0-9a-f]{64}$') {
    throw 'Expected full immutable Phase 5 generator ECR image URI.'
}
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
. (Join-Path $PSScriptRoot 'phase5_aws_common.ps1')
$config = Get-Content -LiteralPath (Join-Path $root 'config/aws-demo.json') -Raw | ConvertFrom-Json
$work = $config.phase5_generator
$common = Assert-Phase5Account $config
$td = Get-Phase5TaskDefinition -Family $work.task_family -Container $work.container -ImageDigest $ImageUri -Common $common
Write-Host "Task: $($td.taskDefinitionArn)"
Write-Host "Run ID: $RunId | orders: $Count"
Write-Host "Mode: $(if ($Apply) {'WRITE TO SHOPIFY'} else {'READ-ONLY PREVIEW'})"
if (-not $Execute) { Write-Host 'PREVIEW ONLY. Add -Execute (and -Apply for orders).'; return }
$command = @('--run-id',$RunId,'--count',"$Count")
if ($Apply) {
    $confirm = Read-Host "Type CREATE-$Count-SYNTHETIC-ORDERS to authorize real Shopify order creation"
    if ($confirm -cne "CREATE-$Count-SYNTHETIC-ORDERS") { throw 'Write authorization missing.' }
    $command += @('--apply','--confirm-run-id',$RunId)
}
Invoke-Phase5Task -Config $config -Work $work -TaskDefinition $td.taskDefinitionArn -Command $command -Common $common -MaxWaitMinutes $MaxWaitMinutes
