<#
.SYNOPSIS
    Launches the one-off PostgreSQL application-user bootstrap on AWS ECS Fargate.

.DESCRIPTION
    Confirms the ECS task definition is ACTIVE, checks that no other bootstrap task
    is PENDING or RUNNING, and launches one Fargate task in private application
    subnets. Waits for completion and verifies the bootstrap container exit code.

    The container applies customer-sync SQL migrations, creates or updates a
    dedicated PostgreSQL application role, and publishes credentials to AWS
    Secrets Manager. Each successful execution generates a NEW password.
    Rerunning this script therefore rotates credentials.

.FREQUENCY
    Once for initial database setup. Rerun manually only for an approved
    credential rotation or recovery. Never schedule or run automatically from
    Terraform apply, CI, or a recurring job.

.PREREQUISITES
    AWS CLI v2; a configured AWS profile with ecs:DescribeTaskDefinition,
    ecs:ListTasks, ecs:RunTask, ecs:DescribeTasks, and iam:PassRole; deployed
    bootstrap task definition and container image; private application subnets
    with access to the private RDS instance and AWS services.

.EXAMPLE
    .\scripts\aws\run_db_bootstrap.ps1
    Inspects the configuration and existing tasks without launching anything.

.EXAMPLE
    .\scripts\aws\run_db_bootstrap.ps1 -Execute -ConfirmBootstrap
    Launches one bootstrap task, waits for its completion, and checks its exit code.
#>
[CmdletBinding()]
param(
    [switch]$Execute,
    [switch]$ConfirmBootstrap,
    [string]$AwsProfile = 'ecommerce-admin',
    [string]$Region = 'eu-central-1',
    [string]$Cluster = 'ecommerce-demo',
    [string]$TaskFamily = 'ecommerce-db-bootstrap-demo',
    [string]$TaskDefinition = 'ecommerce-db-bootstrap-demo:1',
    [string]$SecurityGroup = 'sg-0128bc9b086b7072b',
    [string[]]$Subnets = @('subnet-0248e35f98ea3fbf6', 'subnet-0481cb476d153d5ba')
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-AwsJson {
    <#
    .SYNOPSIS
        Executes an AWS CLI command and parses its JSON response.
    .DESCRIPTION
        Raises an error on AWS CLI failure. Intended for operational metadata,
        not commands that retrieve secret values.
    #>
    param([Parameter(Mandatory = $true)][string[]]$CliArgs)

    $output = & aws @CliArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "AWS CLI request failed: $($CliArgs[0]) $($CliArgs[1]). $($output -join ' ')"
    }
    return (($output -join "`n") | ConvertFrom-Json)
}

if ($Execute -and -not $ConfirmBootstrap) {
    throw 'Bootstrap modifies PostgreSQL and rotates its application password. Add -ConfirmBootstrap to proceed.'
}

$baseArgs = @('--region', $Region, '--profile', $AwsProfile)
$definition = Invoke-AwsJson -CliArgs (@('ecs', 'describe-task-definition', '--task-definition', $TaskDefinition) + $baseArgs)

if ($definition.taskDefinition.status -ne 'ACTIVE') {
    throw 'ECS task definition is not ACTIVE.'
}
if ($definition.taskDefinition.family -ne $TaskFamily) {
    throw 'ECS task family does not match the expected bootstrap family.'
}

foreach ($status in @('PENDING', 'RUNNING')) {
    $existing = Invoke-AwsJson -CliArgs (@('ecs', 'list-tasks', '--cluster', $Cluster, '--family', $TaskFamily, '--desired-status', $status) + $baseArgs)
    if (@($existing.taskArns).Count -gt 0) {
        throw "Bootstrap task already $status. Do not launch another task. ARNs: $($existing.taskArns -join ', ')"
    }
}

Write-Host "Cluster: $Cluster"
Write-Host "Task definition: $($definition.taskDefinition.taskDefinitionArn)"
Write-Host "Private subnets: $($Subnets -join ', ')"
Write-Host "Security group: $SecurityGroup"
Write-Host 'Public IP: DISABLED'
Write-Host 'No PENDING or RUNNING bootstrap task found.'

if (-not $Execute) {
    Write-Host 'PREVIEW ONLY. Use -Execute -ConfirmBootstrap to launch.'
    return
}

$network = @{
    awsvpcConfiguration = @{
        subnets = $Subnets
        securityGroups = @($SecurityGroup)
        assignPublicIp = 'DISABLED'
    }
}
$tempFile = Join-Path ([System.IO.Path]::GetTempPath()) ("ecs-bootstrap-network-{0}.json" -f [guid]::NewGuid())

try {
    $network | ConvertTo-Json -Depth 5 -Compress |
        Set-Content -LiteralPath $tempFile -Encoding ascii

    $fileUri = 'file://' + ($tempFile -replace '\\', '/')
    $response = Invoke-AwsJson -CliArgs (@('ecs', 'run-task', '--cluster', $Cluster, '--task-definition', $TaskDefinition, '--launch-type', 'FARGATE', '--count', '1', '--network-configuration', $fileUri) + $baseArgs)

    if (@($response.failures).Count -gt 0 -or @($response.tasks).Count -ne 1) {
        throw "ECS task launch was not accepted: $($response.failures | ConvertTo-Json -Compress)"
    }

    $taskArn = $response.tasks[0].taskArn
    Write-Host "Launched task: $taskArn"
    Write-Host 'Waiting for task completion...'

    & aws ecs wait tasks-stopped --cluster $Cluster --tasks $taskArn @baseArgs
    if ($LASTEXITCODE -ne 0) {
        throw 'Wait failed or timed out. Inspect the ECS task before attempting a rerun.'
    }

    $taskInfo = Invoke-AwsJson -CliArgs (@('ecs', 'describe-tasks', '--cluster', $Cluster, '--tasks', $taskArn) + $baseArgs)
    $task = $taskInfo.tasks[0]
    $containers = @($task.containers | Where-Object { $_.name -eq 'db-bootstrap' })
    if ($containers.Count -ne 1) {
        throw 'Expected db-bootstrap container was not found in ECS task result.'
    }
    $container = $containers[0]

    Write-Host "Task status: $($task.lastStatus); stop reason: $($task.stoppedReason)"
    Write-Host "Container exit code: $($container.exitCode)"

    if ($null -eq $container.exitCode -or [int]$container.exitCode -ne 0) {
        throw 'Bootstrap did not succeed. Inspect CloudWatch logs before any rerun.'
    }

    Write-Host 'Bootstrap completed. Verify the application secret AWSCURRENT version separately.'
}
finally {
    Remove-Item -LiteralPath $tempFile -Force -ErrorAction SilentlyContinue
}
