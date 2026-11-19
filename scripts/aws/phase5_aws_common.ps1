# Shared Phase 5 AWS CLI functions. Dot-source from the run scripts.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Phase5AwsJson {
    param([Parameter(Mandatory)][string[]]$Arguments)
    $lines = & aws @Arguments --output json
    if ($LASTEXITCODE -ne 0) { throw "AWS CLI request failed: $($Arguments[0]) $($Arguments[1])" }
    return (($lines -join "`n") | ConvertFrom-Json)
}

function Assert-Phase5Account {
    param($Config)
    $common = @('--profile', [string]$Config.profile, '--region', [string]$Config.region)
    $identity = Invoke-Phase5AwsJson (@('sts','get-caller-identity') + $common)
    if ([string]$identity.Account -cne [string]$Config.account_id) {
        throw "Unexpected AWS account. No task launched."
    }
    return $common
}

function Get-Phase5TaskDefinition {
    param([string]$Family, [string]$Container, [string]$ImageDigest, [string[]]$Common)
    $response = Invoke-Phase5AwsJson (@('ecs','describe-task-definition','--task-definition',$Family) + $Common)
    $definition = $response.taskDefinition
    if ($definition.status -ne 'ACTIVE' -or $definition.family -ne $Family) {
        throw 'Task definition missing, inactive or unexpected.'
    }
    $matches = @($definition.containerDefinitions | Where-Object { $_.name -eq $Container })
    if ($matches.Count -ne 1) { throw 'Unexpected ECS container definition.' }
    if ($matches[0].image -cne $ImageDigest) {
        throw 'Task image digest does not match the pinned approved image.'
    }
    return $definition
}

function Assert-Phase5NoConcurrentTask {
    param([string]$Cluster, [string]$Family, [string[]]$Common)
    foreach ($desired in @('RUNNING','PENDING')) {
        $active = Invoke-Phase5AwsJson (@('ecs','list-tasks','--cluster',$Cluster,
                                            '--family',$Family,'--desired-status',$desired) + $Common)
        if (@($active.taskArns).Count -gt 0) {
            throw "Another $Family task is $desired. Refusing concurrent execution."
        }
    }
}

function Write-Phase5JsonFile {
    param($Payload, [string]$Prefix)
    $file = Join-Path ([IO.Path]::GetTempPath()) ($Prefix + '-' + [guid]::NewGuid() + '.json')
    [IO.File]::WriteAllText($file, ($Payload | ConvertTo-Json -Depth 10 -Compress),
                            [Text.UTF8Encoding]::new($false))
    return $file
}

function Invoke-Phase5Task {
    param($Config, $Work, [string]$TaskDefinition, [string[]]$Command,
          [string[]]$Common, [int]$MaxWaitMinutes = 180)
    Assert-Phase5NoConcurrentTask -Cluster $Config.cluster -Family $Work.task_family -Common $Common
    $network = @{ awsvpcConfiguration = @{
        subnets = @($Work.subnets)
        securityGroups = @($Work.security_groups)
        assignPublicIp = 'DISABLED'
    }}
    $overrides = $null
    if ($null -ne $Command -and @($Command).Count -gt 0) {
        $overrides = @{ containerOverrides = @(@{
            name = [string]$Work.container
            command = @($Command)
        }) }
    }
    $networkFile = $null
    $overrideFile = $null
    try {
        $networkFile = Write-Phase5JsonFile $network 'phase5-network'
        $networkUri = 'file://' + $networkFile.Replace('\','/')
        $launchArgs = @(
            'ecs','run-task','--cluster',[string]$Config.cluster,
            '--task-definition',$TaskDefinition,'--launch-type','FARGATE',
            '--count','1','--network-configuration',$networkUri
        )
        if ($null -ne $overrides) {
            $overrideFile = Write-Phase5JsonFile $overrides 'phase5-overrides'
            $overrideUri = 'file://' + $overrideFile.Replace('\','/')
            $launchArgs += @('--overrides',$overrideUri)
        }
        $started = Invoke-Phase5AwsJson ($launchArgs + $Common)
        if (@($started.failures).Count -ne 0 -or @($started.tasks).Count -ne 1) {
            throw 'ECS did not accept exactly one task.'
        }
        $taskArn = [string]$started.tasks[0].taskArn
        Write-Host "ECS task launched: $taskArn"
        $deadline = (Get-Date).AddMinutes($MaxWaitMinutes)
        do {
            $status = Invoke-Phase5AwsJson (@('ecs','describe-tasks',
                '--cluster',[string]$Config.cluster,'--tasks',$taskArn) + $Common)
            $task = @($status.tasks)[0]
            if ($task.lastStatus -eq 'STOPPED') { break }
            if ((Get-Date) -ge $deadline) {
                throw "Task is still running: $taskArn. Inspect before retrying."
            }
            Start-Sleep -Seconds 20
        } while ($true)
        $containers = @($task.containers | Where-Object { $_.name -eq $Work.container })
        if ($containers.Count -ne 1 -or $null -eq $containers[0].exitCode) {
            throw "Missing container exit code. Inspect task $taskArn."
        }
        $code = [int]$containers[0].exitCode
        Write-Host "Task: $($task.lastStatus); exit code: $code"
        $taskId = $taskArn.Split('/')[-1]
        $stream = "ecs/$($Work.container)/$taskId"
        try {
            $logs = Invoke-Phase5AwsJson (@('logs','get-log-events',
                '--log-group-name',[string]$Work.log_group,
                '--log-stream-name',$stream,'--start-from-head') + $Common)
            foreach ($event in @($logs.events)) { Write-Host ([string]$event.message) }
        } catch {
            Write-Warning "Task logs not yet available. CloudWatch stream: $stream"
        }
        if ($code -ne 0) { throw "Task failed. Reconcile before retry: $taskArn" }
        return $taskArn
    } finally {
        foreach ($file in @($networkFile,$overrideFile)) {
            if ($file) { Remove-Item -LiteralPath $file -Force -ErrorAction SilentlyContinue }
        }
    }
}
