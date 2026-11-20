<#
.SYNOPSIS
    Deploy the corrected Phase 6 evening-coordinator Lambda ZIP.
.DESCRIPTION
    Runs from the repository root or any working directory. Packages the
    tracked Python coordinator as index.py, validates the ZIP locally, then
    uploads it using the Windows-tested AWS CLI fileb:// convention.
    Deploys ONLY Lambda code: no Terraform, order generation, RDS start,
    Glue job launch, or Redshift modification.
.EXAMPLE
    .\scripts\aws\deploy_phase6_evening_coordinator.ps1 -ValidateOnly
    Package and validate locally without AWS calls.
.EXAMPLE
    .\scripts\aws\deploy_phase6_evening_coordinator.ps1
    Upload the verified ZIP and confirm AWS deployed its exact SHA-256.
#>
[CmdletBinding()]
param([switch]$ValidateOnly)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$AwsProfile = 'ecommerce-admin'
$Region = 'eu-central-1'
$FunctionName = 'ecommerce-phase6-evening-glue-coordinator-demo'
$Source = Join-Path $Root 'scripts\aws\phase6_evening_coordinator.py'
$Temp = Join-Path ([IO.Path]::GetTempPath()) ('phase6-coordinator-' + [guid]::NewGuid().ToString('N'))

Push-Location $Root
try {
    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Coordinator source not found: $Source"
    }
    $sourceText = [IO.File]::ReadAllText($Source)
    if (-not $sourceText.Contains('events = dynamodb.Table(EVENTS_TABLE)') -or
        -not $sourceText.Contains('ConditionExpression="#day=:day AND #owner=:owner"')) {
        throw 'Coordinator source does not contain the committed DynamoDB fixes'
    }
    & python -c 'import ast, pathlib, sys; ast.parse(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))' $Source
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
    if ($files.Count -ne 1 -or $files[0].Name -ne 'index.py') {
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
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $Temp -Recurse -Force -ErrorAction SilentlyContinue
}
