<#
On-demand, guarded ECS customer synchronization run. Preview-only by default.
Use AFTER an approved Terraform task definition/image deployment, not to redo
the completed 2026-11-18 population. Never invoked by CI or Terraform apply.
Examples:
  .\scripts\aws\run_customer_sync.ps1
  .\scripts\aws\run_customer_sync.ps1 -Execute
  .\scripts\aws\run_customer_sync.ps1 -Execute -AllowWrites
#>
[CmdletBinding()]
param(
    [string]$ConfigPath = 'config/aws-demo.json',
    [string]$TaskDefinition = 'ecommerce-customer-sync-demo',
    [ValidateRange(0, 10000000)][int]$Limit = 5,
    [ValidateRange(1, 1000)][int]$BatchSize = 100,
    [switch]$Execute,
    [switch]$AllowWrites,
    [switch]$Wait
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
function Invoke-AwsJson([string[]]$Arguments) {
    $result = & aws @Arguments --output json
    if ($LASTEXITCODE -ne 0) { throw 'AWS CLI command failed. No automatic retry.' }
    return ($result | ConvertFrom-Json)
}
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$config = Get-Content (Join-Path $root $ConfigPath) -Raw | ConvertFrom-Json
$work = $config.customer_sync
$common = @('--region', "$($config.region)", '--profile', "$($config.profile)")
$account = & aws sts get-caller-identity @common --query Account --output text
if ($LASTEXITCODE -ne 0 -or "$account".Trim() -ne "$($config.account_id)") {
    throw 'AWS account validation failed.'
}
$definition = Invoke-AwsJson (@('ecs','describe-task-definition','--task-definition',$TaskDefinition) + $common)
$td = $definition.taskDefinition
if ($td.status -ne 'ACTIVE' -or $td.family -ne "$($work.task_family)") {
    throw 'Unexpected or inactive task definition.'
}
$containers = @($td.containerDefinitions | Where-Object { $_.name -eq "$($work.container)" })
if ($containers.Count -ne 1) { throw 'Customer-sync container not found.' }
Write-Host "Task definition: $($td.taskDefinitionArn)"
Write-Host "Image: $($containers[0].image)"
Write-Host "Source bucket: $($config.landing_bucket)"
Write-Host "Selected customers: $Limit; progress batch size: $BatchSize"
Write-Host "Writes: $AllowWrites"
Write-Host 'No automatic Terraform apply, Docker rebuild or retries.'
if (-not $Execute) { Write-Host 'PREVIEW ONLY: use -Execute to launch one task.'; return }

foreach ($status in @('PENDING','RUNNING')) {
    $existing = Invoke-AwsJson (@('ecs','list-tasks','--cluster',"$($config.cluster)",'--family',"$($work.task_family)",'--desired-status',$status) + $common)
    if (@($existing.taskArns).Count -gt 0) {
        throw "Another customer sync task is $status. Do not launch concurrently."
    }
}
if ($AllowWrites) {
    $confirmation = (Read-Host "Type AUTHORIZE-SHOPIFY-WRITES to launch $Limit write-enabled records").Trim()
    if ($confirmation -cne 'AUTHORIZE-SHOPIFY-WRITES') {
        Write-Host 'Cancelled; no ECS task launched.'
        return
    }
}
$cmd = @('--bucket',"$($config.landing_bucket)",'--limit',"$Limit",'--batch-size',"$BatchSize")
if ($AllowWrites) { $cmd += '--allow-customer-writes' }
$overrides = @{ containerOverrides = @(@{ name = "$($work.container)"; command = $cmd }) }
$network = @{ awsvpcConfiguration = @{
    subnets = @($work.subnets)
    securityGroups = @($work.security_groups)
    assignPublicIp = 'DISABLED'
} }
$overrideFile = Join-Path ([IO.Path]::GetTempPath()) ('customer-sync-override-' + [guid]::NewGuid() + '.json')
$networkFile = Join-Path ([IO.Path]::GetTempPath()) ('customer-sync-network-' + [guid]::NewGuid() + '.json')
try {
    [IO.File]::WriteAllText($overrideFile, ($overrides | ConvertTo-Json -Depth 6 -Compress), [Text.UTF8Encoding]::new($false))
    [IO.File]::WriteAllText($networkFile, ($network | ConvertTo-Json -Depth 6 -Compress), [Text.UTF8Encoding]::new($false))
    $launch = Invoke-AwsJson (@('ecs','run-task','--cluster',"$($config.cluster)",'--task-definition',$TaskDefinition,
        '--launch-type','FARGATE','--count','1','--network-configuration',('file://' + $networkFile.Replace('\','/')),
        '--overrides',('file://' + $overrideFile.Replace('\','/'))) + $common)
    if (@($launch.failures).Count -gt 0 -or @($launch.tasks).Count -ne 1) {
        throw 'ECS launch response did not contain exactly one successful task. Inspect AWS before rerun.'
    }
    $taskArn = "$($launch.tasks[0].taskArn)"
    Write-Host "Launched: $taskArn"
    if (-not $Wait) {
        Write-Host 'Check ECS console/CloudWatch for final summary and exit code.'
        return
    }
    # Custom polling supports long imports; the AWS CLI ECS waiter times out early.
    $deadline = (Get-Date).AddHours(3)
    do {
        $taskResult = Invoke-AwsJson (@('ecs','describe-tasks','--cluster',"$($config.cluster)",'--tasks',$taskArn) + $common)
        $task = $taskResult.tasks[0]
        if ($task.lastStatus -eq 'STOPPED') { break }
        if ((Get-Date) -ge $deadline) {
            throw "Task still running after three hours. Inspect $taskArn; do not relaunch."
        }
        Start-Sleep -Seconds 30
    } while ($true)
    $container = @($task.containers | Where-Object { $_.name -eq "$($work.container)" })
    if ($container.Count -ne 1 -or $null -eq $container[0].exitCode) { throw "No container exit code. Inspect $taskArn" }
    Write-Host "Task: $($task.lastStatus); exit code: $($container[0].exitCode)"
    if ([int]$container[0].exitCode -ne 0) { throw "Customer sync failed: $taskArn" }
    Write-Host 'Task completed; check final CloudWatch reconciliation counts separately.'
}
finally {
    Remove-Item $overrideFile,$networkFile -Force -ErrorAction SilentlyContinue
}
