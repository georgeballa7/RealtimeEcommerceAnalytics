<#
.SYNOPSIS
    Guarded, reusable on-demand customer/catalog ECS task launcher.
.DESCRIPTION
    Preview only by default; never builds images, modifies Terraform or
    launches Shopify mutations without interactive write authorization.
    Cloud evidence and approvals belong in the existing Notion runbook.
.EXAMPLE
    .\scripts\aws\run_shopify_sync.ps1 -Workload catalog_sync
.EXAMPLE
    .\scripts\aws\run_shopify_sync.ps1 -Workload catalog_sync -Execute -Wait
.EXAMPLE
    .\scripts\aws\run_shopify_sync.ps1 -Workload catalog_sync -Execute -AllowWrites -ApprovalKey ... -ApprovalSha256 ...
#>
[CmdletBinding()]
param(
    [ValidateSet('customer_sync', 'catalog_sync')]
    [string]$Workload = 'customer_sync',
    [string]$ConfigPath = 'config/aws-demo.json',
    [string]$TaskDefinition,
    [ValidateRange(0, 10000000)][int]$Limit = 5,
    [ValidateRange(1, 1000)][int]$BatchSize = 100,
    [string]$ApprovalKey,
    [string]$ApprovalSha256,
    [switch]$Execute,
    [switch]$AllowWrites,
    [switch]$Wait
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-AwsJson([string[]]$Arguments) {
    $result = & aws @Arguments --output json
    if ($LASTEXITCODE -ne 0) {
        throw 'AWS CLI operation failed; inspect the task before attempting a new launch.'
    }
    return (($result -join "`n") | ConvertFrom-Json)
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$config = Get-Content -LiteralPath (Join-Path $root $ConfigPath) -Raw | ConvertFrom-Json
$work = $config.$Workload
$common = @('--profile', "$($config.profile)", '--region', "$($config.region)")
$account = & aws sts get-caller-identity @common --query Account --output text
if ($LASTEXITCODE -ne 0 -or "$account".Trim() -ne "$($config.account_id)") {
    throw 'Incorrect AWS profile/account; nothing launched.'
}
if (-not $TaskDefinition) {
    $TaskDefinition = "$($work.task_family)"
}
$definition = Invoke-AwsJson (@('ecs', 'describe-task-definition',
    '--task-definition', $TaskDefinition) + $common)
$td = $definition.taskDefinition
if ($td.status -ne 'ACTIVE' -or $td.family -ne "$($work.task_family)") {
    throw 'Task definition must be ACTIVE and from the expected workload family.'
}
$container = @($td.containerDefinitions | Where-Object { $_.name -eq "$($work.container)" })
if ($container.Count -ne 1) { throw 'Expected application container is missing.' }
$imageRef = [string]$container[0].image
$imageParts = $imageRef.Split('@')
if ($imageParts.Count -ne 2 -or -not $imageParts[1].StartsWith('sha256:')) {
    throw 'ECS task image must be pinned to an immutable registry digest.'
}
$imageHash = $imageParts[1].Substring(7)
if ($imageHash.Length -ne 64 -or $imageHash -match '[^0-9a-f]') {
    throw 'Expected a lowercase SHA-256 registry image digest.'
}
if ($Workload -eq 'catalog_sync') {
    if (-not $ApprovalKey) { $ApprovalKey = [string]$work.approval_key }
    if (-not $ApprovalSha256) { $ApprovalSha256 = [string]$work.approval_sha256 }
    if (-not $ApprovalKey -or -not $ApprovalSha256) {
        throw 'Catalog reconciliation requires a pinned S3 approval key and SHA-256.'
    }
    if ($ApprovalKey -notlike 'historical/online_retail_ii/catalog_approval/v1/*') {
        throw 'Unexpected catalog approval S3 prefix.'
    }
    if ($ApprovalSha256 -cnotmatch '^[a-f0-9]{64}$') {
        throw 'ApprovalSha256 must be a lowercase hex SHA-256.'
    }
}
$cmd = @('--bucket', "$($config.landing_bucket)", '--limit', "$Limit")
if ($Workload -eq 'customer_sync') {
    $cmd += @('--batch-size', "$BatchSize")
    if ($AllowWrites) { $cmd += '--allow-customer-writes' }
} else {
    $cmd += @('--approval-key', $ApprovalKey, '--approval-sha256', $ApprovalSha256)
    if ($AllowWrites) { $cmd += '--allow-catalog-writes' }
}
Write-Host "AWS account: $account"
Write-Host "ECS cluster: $($config.cluster)"
Write-Host "Task definition: $($td.taskDefinitionArn)"
Write-Host "Pinned task image: $($container[0].image)"
Write-Host "Private subnets: $($work.subnets -join ', ')"
Write-Host "Source: $($config.landing_bucket)"
Write-Host "Workload: $Workload | selected limit: $Limit | writes enabled: $AllowWrites"
if (-not $Execute) { Write-Host 'PREVIEW ONLY: add -Execute to launch exactly one task.'; return }

foreach ($status in @('PENDING', 'RUNNING')) {
    $tasks = Invoke-AwsJson (@('ecs', 'list-tasks', '--cluster', "$($config.cluster)",
        '--family', "$($work.task_family)", '--desired-status', $status) + $common)
    if (@($tasks.taskArns).Count -gt 0) {
        throw "A same-family ECS task is $status. Do not run two synchronization tasks."
    }
}
if ($AllowWrites) {
    $approval = Read-Host "Type AUTHORIZE-SHOPIFY-WRITES to proceed"
    if ($approval -cne 'AUTHORIZE-SHOPIFY-WRITES') {
        Write-Host 'Cancelled: no ECS task created.'
        return
    }
}

$network = @{ awsvpcConfiguration = @{
    subnets = @($work.subnets)
    securityGroups = @($work.security_groups)
    assignPublicIp = 'DISABLED'
} }
$overrides = @{ containerOverrides = @(@{
    name = "$($work.container)"
    command = $cmd
}) }
$networkFile = Join-Path ([IO.Path]::GetTempPath()) ('sync-network-' + [guid]::NewGuid() + '.json')
$overrideFile = Join-Path ([IO.Path]::GetTempPath()) ('sync-override-' + [guid]::NewGuid() + '.json')
try {
    [IO.File]::WriteAllText($networkFile,
        ($network | ConvertTo-Json -Depth 6 -Compress), [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($overrideFile,
        ($overrides | ConvertTo-Json -Depth 6 -Compress), [Text.UTF8Encoding]::new($false))
    $networkUri = 'file://' + $networkFile.Replace('\', '/')
    $overrideUri = 'file://' + $overrideFile.Replace('\', '/')
    $launch = Invoke-AwsJson (@('ecs', 'run-task',
        '--cluster', "$($config.cluster)", '--task-definition', "$($td.taskDefinitionArn)",
        '--launch-type', 'FARGATE', '--count', '1', '--network-configuration', $networkUri,
        '--overrides', $overrideUri) + $common)
    if (@($launch.failures).Count -gt 0 -or @($launch.tasks).Count -ne 1) {
        throw 'ECS rejected task launch. Inspect failures before attempting another run.'
    }
    $taskArn = "$($launch.tasks[0].taskArn)"
    Write-Host "Launched task: $taskArn"
    if (-not $Wait) {
        Write-Host "Monitor ECS and CloudWatch group: $($work.log_group)"
        return
    }
    $deadline = (Get-Date).AddHours(3)
    do {
        $result = Invoke-AwsJson (@('ecs', 'describe-tasks',
            '--cluster', "$($config.cluster)", '--tasks', $taskArn) + $common)
        $task = $result.tasks[0]
        if ($task.lastStatus -eq 'STOPPED') { break }
        if ((Get-Date) -gt $deadline) {
            throw "Task still running; inspect $taskArn before any retry."
        }
        Start-Sleep -Seconds 30
    } while ($true)
    $actual = @($task.containers | Where-Object { $_.name -eq "$($work.container)" })
    if ($actual.Count -ne 1 -or $null -eq $actual[0].exitCode) {
        throw "No final container exit code: inspect $taskArn"
    }
    Write-Host "ECS final status: $($task.lastStatus)"
    Write-Host "Container exit code: $($actual[0].exitCode)"
    if ([int]$actual[0].exitCode -ne 0) {
        throw "Synchronization failed. Do not automatically rerun: $taskArn"
    }
    Write-Host 'Check complete reconciliation counts in CloudWatch before acceptance.'
}
finally {
    Remove-Item -LiteralPath $networkFile,$overrideFile -Force -ErrorAction SilentlyContinue
}
