<#
.SYNOPSIS
    Deploy the corrected Phase 6 dual-slot coordinator Lambda ZIP.
.DESCRIPTION
    Runs from the repository root or any working directory. Packages the
    tracked Python coordinator as index.py, validates the ZIP locally, then
    uploads it using the Windows-tested AWS CLI fileb:// convention.
    Updates ONLY this Lambda code and its existing readiness-check schedule.
    No Terraform, order generation, RDS start, Glue job, or Redshift modification.
.EXAMPLE
    .\scripts\aws\deploy_phase6_evening_coordinator.ps1 -ValidateOnly
    Package and validate locally without AWS calls.
.EXAMPLE
    .\scripts\aws\deploy_phase6_evening_coordinator.ps1
    Upload the verified ZIP and update the existing readiness schedule to cover
    both 08:00 and 16:00 ingestion slots (one poll every five minutes).
#>
[CmdletBinding()]
param([switch]$ValidateOnly)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$AwsProfile = 'ecommerce-admin'
$Region = 'eu-central-1'
$FunctionName = 'ecommerce-phase6-evening-glue-coordinator-demo'
$ScheduleName = 'ecommerce-phase6-evening-glue-demo'
$DesiredExpression = 'cron(0/5 8-21 * * ? *)'
$Source = Join-Path $Root 'scripts\aws\phase6_evening_coordinator.py'
$Temp = Join-Path ([IO.Path]::GetTempPath()) ('phase6-coordinator-' + [guid]::NewGuid().ToString('N'))

Push-Location $Root
try {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Coordinator source not found: $Source"
    }
    $sourceText = [IO.File]::ReadAllText($Source)
    if (-not $sourceText.Contains('events = dynamodb.Table(EVENTS_TABLE)') -or
        -not $sourceText.Contains('ConditionExpression="#day=:day AND #owner=:owner"') -or
        -not $sourceText.Contains('SLOT_IDS = {"morning": "phase6-morning", "afternoon": "phase6-evening"}')) {
        throw 'Coordinator source does not contain the committed DynamoDB fixes'
    }
    & python -X utf8 -c 'import ast,sys;ast.parse(open(sys.argv[1]).read())' $Source
    if ($LASTEXITCODE -ne 0) { throw 'Coordinator Python syntax check failed' }

    New-Item -ItemType Directory -Path $Temp -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination (Join-Path $Temp 'index.py')
    $zip = Join-Path $Temp 'coordinator.zip'
    Compress-Archive -LiteralPath (Join-Path $Temp 'index.py') -DestinationPath $zip -CompressionLevel Optimal
    if (-not (Test-Path -LiteralPath $zip -PathType Leaf)) { throw 'Lambda ZIP packaging failed' }

    # Verify the EXACT same artifact that will be handed to the AWS CLI.
    $verifyDir = Join-Path $Temp 'verify'
    Expand-Archive -LiteralPath $zip -DestinationPath $verifyDir
    $files = @(Get-ChildItem -LiteralPath $verifyDir -File -Recurse)
    if ($files.Count -ne 1 -or $files[0].FullName -ne (Join-Path $verifyDir 'index.py')) {
        throw 'Lambda ZIP must contain one root-level index.py'
    }
    if ((Get-FileHash -LiteralPath $Source -Algorithm SHA256).Hash -ne
        (Get-FileHash -LiteralPath $files[0].FullName -Algorithm SHA256).Hash) {
        throw 'Lambda ZIP content differs from reviewed Python source'
    }
    Write-Host 'Coordinator Python syntax and Lambda ZIP contents validated.'
    if ($ValidateOnly) {
        Write-Host 'VALIDATION ONLY: No AWS calls or changes.'
        return
    }

    $account = (& aws sts get-caller-identity --profile $AwsProfile --region $Region --query Account --output text | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $account -ne '825414820081') {
        throw 'AWS account check failed'
    }
    $currentJson = & aws lambda get-function-configuration --function-name $FunctionName --profile $AwsProfile --region $Region --output json
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect existing Lambda configuration' }
    $current = ($currentJson | Out-String | ConvertFrom-Json)
    if ($current.PackageType -ne 'Zip' -or $current.Runtime -ne 'python3.12' -or
        $current.Handler -ne 'index.handler') {
        throw 'Unexpected Lambda packaging, runtime or handler; code upload stopped'
    }
    $sha = [Security.Cryptography.SHA256]::Create()
    try {
        $expectedHash = [Convert]::ToBase64String($sha.ComputeHash([IO.File]::ReadAllBytes($zip)))
    }
    finally {
        $sha.Dispose()
    }
    # fileb:// reads the ZIP as binary. The slash normalization is the same
    # Windows AWS CLI file:// convention used in run_db_bootstrap.ps1.
    $fileUri = 'fileb://' + ($zip -replace '\\', '/')
    $resultJson = & aws lambda update-function-code --function-name $FunctionName --zip-file $fileUri --profile $AwsProfile --region $Region --output json
    if ($LASTEXITCODE -ne 0) { throw 'Lambda code upload failed' }
    $result = ($resultJson | Out-String | ConvertFrom-Json)
    if ($result.CodeSha256 -ne $expectedHash) {
        throw 'Uploaded Lambda code hash did not match the ZIP artifact'
    }

    & aws lambda wait function-updated --function-name $FunctionName --profile $AwsProfile --region $Region
    if ($LASTEXITCODE -ne 0) { throw 'Lambda update waiter failed' }

    $updatedJson = & aws lambda get-function-configuration --function-name $FunctionName --profile $AwsProfile --region $Region --output json
    if ($LASTEXITCODE -ne 0) { throw 'Could not verify deployed Lambda' }
    $updated = ($updatedJson | Out-String | ConvertFrom-Json)
    if ($updated.LastUpdateStatus -ne 'Successful' -or
        $updated.State -ne 'Active' -or $updated.CodeSha256 -ne $expectedHash) {
        throw 'Lambda status or deployed code SHA-256 did not match'
    }
    Write-Host "Phase 6 coordinator Lambda updated and verified: $($updated.FunctionName)"
    Write-Host "Verified Lambda ZIP SHA-256: $expectedHash"

    # Preserve the already working EventBridge Scheduler target, execution
    # role, input, retry settings, timezone, and OFF flexible window.
    # This is the existing readiness poll, NOT an order-generation schedule.
    $scheduleJson = & aws scheduler get-schedule --name $ScheduleName --profile $AwsProfile --region $Region --output json
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect current Glue readiness schedule' }
    $schedule = ($scheduleJson | Out-String | ConvertFrom-Json)
    $expectedTarget = ('arn:aws:lambda:{0}:825414820081:function:{1}' -f $Region, $FunctionName)
    if ($schedule.Target.Arn -ne $expectedTarget -or
        $schedule.State -ne 'ENABLED' -or
        $schedule.ScheduleExpressionTimezone -ne 'Europe/Berlin' -or
        $schedule.FlexibleTimeWindow.Mode -ne 'OFF') {
        throw 'Unexpected existing coordinator schedule settings; update stopped'
    }
    $previousExpression = 'cron(0/5 16-21 * * ? *)'
    if ($schedule.ScheduleExpression -notin @($previousExpression, $DesiredExpression)) {
        throw "Unexpected current readiness cadence: $($schedule.ScheduleExpression)"
    }
    if ($schedule.ScheduleExpression -ne $DesiredExpression) {
        $update = [ordered]@{
            Name                       = $ScheduleName
            GroupName                  = $schedule.GroupName
            ScheduleExpression         = $DesiredExpression
            ScheduleExpressionTimezone = $schedule.ScheduleExpressionTimezone
            FlexibleTimeWindow         = $schedule.FlexibleTimeWindow
            Target                     = $schedule.Target
            State                      = $schedule.State
            Description                = 'Readiness poll every 5 minutes 08-21 Berlin for 08:00 and 16:00 Shopify slots. No new orders; RDS/Glue to Redshift.'
        }
        foreach ($optional in @('StartDate','EndDate','KmsKeyArn','ActionAfterCompletion')) {
            if ($schedule.PSObject.Properties.Name -contains $optional -and $null -ne $schedule.$optional) {
                $update[$optional] = $schedule.$optional
            }
        }
        $scheduleFile = Join-Path $Temp 'schedule.json'
        [IO.File]::WriteAllText(
            $scheduleFile,
            ($update | ConvertTo-Json -Depth 16 -Compress),
            (New-Object System.Text.UTF8Encoding($false))
        )
        $scheduleUri = 'file://' + ($scheduleFile -replace '\\', '/')
        & aws scheduler update-schedule --cli-input-json $scheduleUri --profile $AwsProfile --region $Region --output json | Out-Host
        if ($LASTEXITCODE -ne 0) {
            throw 'Coordinator Lambda deployed, but readiness schedule update FAILED; rerun after inspecting Scheduler'
        }
    }
    $verifiedExpression = (& aws scheduler get-schedule --name $ScheduleName --profile $AwsProfile --region $Region --query ScheduleExpression --output text | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $verifiedExpression -ne $DesiredExpression) {
        throw 'Coordinator Lambda deployed, but the 08:00/16:00 readiness schedule could not be verified'
    }
    Write-Host 'Verified both generator schedules remain separate; readiness poll now covers 08:00-21:59 Europe/Berlin.'
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $Temp -Recurse -Force -ErrorAction SilentlyContinue
}
