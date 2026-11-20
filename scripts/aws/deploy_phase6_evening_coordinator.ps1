<#
Deploy the existing Phase 6 evening coordinator Lambda from the reviewed
repository source. The Lambda uses a ZIP package rather than an ECR image.
No Terraform plan, new AWS resources or job invocation.
Run from anywhere on Windows after checking out the intended feature branch.
#>
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Profile = 'ecommerce-admin'
$Region = 'eu-central-1'
$Function = 'ecommerce-phase6-evening-glue-coordinator-demo'
$Source = Join-Path $Root 'scripts\aws\phase6_evening_coordinator.py'
$Temp = Join-Path ([IO.Path]::GetTempPath()) ('phase6-coordinator-' + [guid]::NewGuid().ToString('N'))

Push-Location $Root
try {
    $account = (& aws sts get-caller-identity --profile $Profile --region $Region --query Account --output text | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $account -ne '825414820081') {
        throw 'AWS account check failed'
    }
    $code = [IO.File]::ReadAllText($Source)
    if (-not $code.Contains('events = dynamodb.Table(EVENTS_TABLE)') -or
        -not $code.Contains('ConditionExpression="#day=:day AND #owner=:owner"')) {
        throw 'Coordinator source does not contain the committed Phase 6 DynamoDB fixes'
    }
    New-Item -ItemType Directory -Path $Temp | Out-Null
    Copy-Item $Source (Join-Path $Temp 'index.py')
    $zip = Join-Path $Temp 'coordinator.zip'
    Compress-Archive -LiteralPath (Join-Path $Temp 'index.py') -DestinationPath $zip
    if (-not (Test-Path $zip)) { throw 'Lambda ZIP packaging failed' }
    $fileUri = 'fileb://' + ($zip -replace '\\', '/')
    & aws lambda update-function-code --function-name $Function --zip-file $fileUri --profile $Profile --region $Region --output json | Out-Host
    if ($LASTEXITCODE -ne 0) { throw 'Lambda code upload failed' }
    & aws lambda wait function-updated --function-name $Function --profile $Profile --region $Region
    if ($LASTEXITCODE -ne 0) { throw 'Lambda update waiter failed' }
    $status = (& aws lambda get-function-configuration --function-name $Function --profile $Profile --region $Region --query LastUpdateStatus --output text | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $status -ne 'Successful') { throw "Lambda update status: $status" }
    Write-Host 'Phase 6 evening coordinator Lambda updated successfully.'
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $Temp -Recurse -Force -ErrorAction SilentlyContinue
}
