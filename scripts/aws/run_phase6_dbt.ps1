<#
Launch the Terraform-created dbt ECS task on demand (never a service).
Default is preview. -Execute -Action AuthCheck only tests Redshift connectivity.
-Execute -Action Build executes the dimensional core and its dbt tests after
the reviewed SQL grants migration has been applied. Uses the existing private
application subnets and the new dbt task SG; no public IP.
#>
[CmdletBinding()]
param(
    [ValidateSet('AuthCheck', 'Build')]
    [string]$Action = 'AuthCheck',
    [switch]$Execute
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$TerraformDir = Join-Path $Root 'infrastructure\terraform'
$Profile = 'ecommerce-admin'
$Region = 'eu-central-1'
$Cluster = 'ecommerce-demo'
$TempFile = $null

function Assert-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed ($LASTEXITCODE)" }
}
function TerraformOutput([string]$Key) {
    $v = & terraform output -raw $Key
    Assert-Exit "terraform output $Key"
    $value = ($v | Out-String).Trim()
    if (-not $value -or $value -eq 'null') { throw "Missing Terraform output: $Key" }
    return $value
}

Push-Location $TerraformDir
try {
    $account = (& aws sts get-caller-identity --profile $Profile --region $Region --query Account --output text | Out-String).Trim()
    Assert-Exit 'AWS identity'
    if ($account -ne '825414820081') { throw 'Wrong AWS account' }

    $TaskDef = TerraformOutput 'phase6_dbt_task_definition'
    $SecurityGroup = TerraformOutput 'phase6_dbt_security_group_id'
    $SubnetsJson = & terraform output -json application_subnet_ids
    Assert-Exit 'Terraform application subnets'
    $Subnets = @($SubnetsJson | ConvertFrom-Json)
    if ($Subnets.Count -ne 2) { throw 'Expected two private application subnets' }
    if ($TaskDef -notmatch ':task-definition/ecommerce-phase6-dbt-demo:[0-9]+$' -or
        $SecurityGroup -notmatch '^sg-[0-9a-f]+$') {
        throw 'Unexpected dbt task definition or task security group'
    }
    $TaskInfoJson = & aws ecs describe-task-definition --task-definition $TaskDef --profile $Profile --region $Region --output json
    Assert-Exit 'Describe dbt task'
    $TaskInfo = ($TaskInfoJson | Out-String | ConvertFrom-Json).taskDefinition
    if ($TaskInfo.status -ne 'ACTIVE' -or @($TaskInfo.containerDefinitions).Count -ne 1 -or
        $TaskInfo.containerDefinitions[0].name -ne 'phase6-dbt') {
        throw 'Unexpected deployed dbt ECS task definition'
    }
    foreach ($status in @('RUNNING','PENDING')) {
        $existingJson = & aws ecs list-tasks --cluster $Cluster --family ecommerce-phase6-dbt-demo --desired-status $status --profile $Profile --region $Region --output json
        Assert-Exit 'Check existing dbt tasks'
        if (@(($existingJson | Out-String | ConvertFrom-Json).taskArns).Count -gt 0) {
            throw "Another dbt task is already $status"
        }
    }
    $Command = if ($Action -eq 'Build') {
        @('--action','build','--select','+fct_refund_lines','+fct_cancellations')
    } else {
        @('--action','auth-check')
    }
    Write-Host "dbt task: $TaskDef"
    Write-Host "Action: $Action"
    Write-Host "Private subnet count: $($Subnets.Count)"
    Write-Host "Task security group: $SecurityGroup"
    Write-Host 'Public IP: DISABLED'
    if (-not $Execute) {
        Write-Host 'PREVIEW ONLY. Add -Execute to start one temporary ECS dbt task.'
        return
    }
    $Overrides = @{containerOverrides = @(@{name = 'phase6-dbt'; command = $Command})}
    $Network = @{awsvpcConfiguration = @{
        subnets = $Subnets
        securityGroups = @($SecurityGroup)
        assignPublicIp = 'DISABLED'
    }}
    $TempFile = Join-Path ([IO.Path]::GetTempPath()) ("phase6-dbt-task-" + [guid]::NewGuid().ToString('N') + '.json')
    $Json = @{containerOverrides = $Overrides.containerOverrides}
    # The AWS ECS overrides file and network settings are separate JSON options.
    $OverridesFile = $TempFile
    $NetworkFile = $TempFile + '.network'
    [IO.File]::WriteAllText($OverridesFile, ($Json | ConvertTo-Json -Depth 8 -Compress), (New-Object Text.UTF8Encoding($false)))
    [IO.File]::WriteAllText($NetworkFile, ($Network | ConvertTo-Json -Depth 8 -Compress), (New-Object Text.UTF8Encoding($false)))
    $OverrideUri = 'file://' + ($OverridesFile -replace '\\', '/')
    $NetworkUri = 'file://' + ($NetworkFile -replace '\\', '/')
    $resultJson = & aws ecs run-task --cluster $Cluster --task-definition $TaskDef --launch-type FARGATE --count 1 --overrides $OverrideUri --network-configuration $NetworkUri --profile $Profile --region $Region --output json
    Assert-Exit 'Start dbt task'
    $result = $resultJson | Out-String | ConvertFrom-Json
    if (@($result.failures).Count -gt 0 -or @($result.tasks).Count -ne 1) {
        throw 'ECS task run was not accepted; review failures'
    }
    $Arn = $result.tasks[0].taskArn
    Write-Host "Started $Action dbt task: $Arn"
    & aws ecs wait tasks-stopped --cluster $Cluster --tasks $Arn --profile $Profile --region $Region
    Assert-Exit 'Wait for dbt task'
    $stoppedJson = & aws ecs describe-tasks --cluster $Cluster --tasks $Arn --profile $Profile --region $Region --output json
    Assert-Exit 'Inspect stopped dbt task'
    $task = ($stoppedJson | Out-String | ConvertFrom-Json).tasks[0]
    $container = @($task.containers | Where-Object { $_.name -eq 'phase6-dbt' })
    if ($container.Count -ne 1 -or $null -eq $container[0].exitCode -or
        [int]$container[0].exitCode -ne 0) {
        throw "dbt task failed: $Arn. Inspect /ecs/ecommerce-demo/phase6-dbt CloudWatch logs."
    }
    Write-Host "dbt $Action completed successfully (container exit 0)."
}
finally {
    Pop-Location
    if ($TempFile) {
        Remove-Item -LiteralPath $TempFile -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath ($TempFile + '.network') -Force -ErrorAction SilentlyContinue
    }
}
