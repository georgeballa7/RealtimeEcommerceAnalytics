<#
Backward-compatible customer synchronization wrapper.
The shared, guarded customer/catalog ECS launcher is run_shopify_sync.ps1.
Preview-only by default; no automatic AWS writes.
#>
[CmdletBinding()]
param(
    [string]$ConfigPath = 'config/aws-demo.json',
    [string]$TaskDefinition,
    [ValidateRange(0, 10000000)][int]$Limit = 5,
    [ValidateRange(1, 1000)][int]$BatchSize = 100,
    [switch]$Execute,
    [switch]$AllowWrites,
    [switch]$Wait
)
$ErrorActionPreference = 'Stop'
$arguments = @{}
foreach ($key in $PSBoundParameters.Keys) {
    $arguments[$key] = $PSBoundParameters[$key]
}
$arguments['Workload'] = 'customer_sync'
& (Join-Path $PSScriptRoot 'run_shopify_sync.ps1') @arguments
