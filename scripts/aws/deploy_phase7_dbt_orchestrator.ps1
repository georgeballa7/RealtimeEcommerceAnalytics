<# Safe one-command deployment for the staged Phase 7 dbt orchestrator.
Preview by default. -Apply registers a launch-DISABLED Lambda and event targets.
#>
[CmdletBinding()]
param([switch]$Apply)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Name = 'ecommerce-phase7-dbt-orchestrator-demo'
$Region = 'eu-central-1'
$Profile = 'ecommerce-admin'
$Account = '825414820081'
$Role = "arn:aws:iam::$($Account):role/$Name"
$Arn = "arn:aws:lambda:$($Region):$($Account):function:$Name"
$Temp = Join-Path ([IO.Path]::GetTempPath()) ('dbt-orch-' + [guid]::NewGuid().ToString('N'))
$Rules = @('ecommerce-phase7-loader-success-dbt-demo',
           'ecommerce-phase7-dbt-task-stopped-demo',
           'ecommerce-phase7-dbt-reconcile-demo')

function Check([string]$Name) {
    if ($LASTEXITCODE -ne 0) { throw "$Name failed (exit code $LASTEXITCODE)" }
}
Push-Location $Root
try {
    $Branch = (& git branch --show-current | Out-String).Trim()
    Check 'Git branch'
    if ($Branch -ne 'feature/dbt-analytics-churn-model') { throw 'Wrong Git branch' }
    if (@(& git status --porcelain).Count -gt 0) { throw 'Git worktree must be clean' }
    $Source = Join-Path $Root 'scripts\aws\phase7_dbt_orchestrator.py'
    & python -X utf8 -c 'import ast,sys;ast.parse(open(sys.argv[1]).read())' $Source
    Check 'Python parser'
    New-Item -ItemType Directory -Path $Temp -Force | Out-Null
    Copy-Item $Source (Join-Path $Temp 'index.py')
    $Zip = Join-Path $Temp 'orchestrator.zip'
    Compress-Archive -LiteralPath (Join-Path $Temp 'index.py') -DestinationPath $Zip
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try { $Expected = [Convert]::ToBase64String($Hasher.ComputeHash([IO.File]::ReadAllBytes($Zip))) }
    finally { $Hasher.Dispose() }
    Write-Host "Source and Lambda ZIP verified: $Expected"
    if (-not $Apply) {
        Write-Host 'PREVIEW ONLY. Use -Apply to create a launch-disabled event orchestrator.'
        return
    }
    $Actual = (& aws sts get-caller-identity --profile $Profile --region $Region --query Account --output text | Out-String).Trim()
    Check 'AWS account check'
    if ($Actual -ne $Account) { throw 'Wrong AWS account' }
    $Existing = (& aws lambda list-functions --profile $Profile --region $Region --query "Functions[?FunctionName=='$Name'].FunctionName | [0]" --output text | Out-String).Trim()
    Check 'Check Lambda absence'
    if ($Existing -and $Existing -ne 'None') { throw 'Lambda already exists. Stop for reconciliation.' }
    $RoleActual = (& aws iam get-role --role-name $Name --profile $Profile --query Role.Arn --output text | Out-String).Trim()
    Check 'Check dedicated IAM role'
    if ($RoleActual -ne $Role) { throw 'IAM role differs from expected' }
    $EnvFile = Join-Path $Temp 'environment.json'
    $Config = @{Variables = @{
        DBT_LAUNCH_ENABLED = 'false'
        DBT_TASK_DEFINITION = "arn:aws:ecs:$($Region):$($Account):task-definition/ecommerce-phase6-dbt-demo:1"
    }}
    [IO.File]::WriteAllText($EnvFile,($Config | ConvertTo-Json -Compress -Depth 6),(New-Object Text.UTF8Encoding($false)))
    $ZipUri = 'fileb://' + ($Zip -replace '\\','/')
    $EnvUri = 'file://' + ($EnvFile -replace '\\','/')
    & aws lambda create-function --function-name $Name --runtime python3.12 --handler index.handler --role $Role --timeout 60 --memory-size 128 --zip-file $ZipUri --environment $EnvUri --region $Region --profile $Profile --output json | Out-Null
    Check 'Create orchestrator Lambda'
    & aws lambda wait function-active --function-name $Name --region $Region --profile $Profile
    Check 'Lambda function active'
    $Cfg = (& aws lambda get-function-configuration --function-name $Name --region $Region --profile $Profile --output json | Out-String | ConvertFrom-Json)
    Check 'Verify Lambda'
    if ($Cfg.CodeSha256 -ne $Expected -or $Cfg.Environment.Variables.DBT_LAUNCH_ENABLED -ne 'false') {
        throw 'Uploaded Lambda digest or launch-disabled state differs'
    }
    foreach ($Rule in $Rules) {
        $RuleArn = (& aws events describe-rule --name $Rule --region $Region --profile $Profile --query Arn --output text | Out-String).Trim()
        Check "Read rule $Rule"
        $Sid = 'allow-' + $Rule
        & aws lambda add-permission --function-name $Name --statement-id $Sid --action lambda:InvokeFunction --principal events.amazonaws.com --source-arn $RuleArn --region $Region --profile $Profile --output json | Out-Null
        Check "Allow rule $Rule"
        $TargetFile = Join-Path $Temp ($Rule + '.json')
        [IO.File]::WriteAllText($TargetFile,(@(@{Id='dbt-reconciler';Arn=$Arn}) | ConvertTo-Json -Depth 4 -Compress),(New-Object Text.UTF8Encoding($false)))
        $TargetUri = 'file://' + ($TargetFile -replace '\\','/')
        $Response = (& aws events put-targets --rule $Rule --targets $TargetUri --region $Region --profile $Profile --output json | Out-String | ConvertFrom-Json)
        Check "Attach rule $Rule"
        if ($Response.FailedEntryCount -ne 0) { throw "Target not accepted: $Rule" }
    }
    & aws events enable-rule --name 'ecommerce-phase7-dbt-reconcile-demo' --region $Region --profile $Profile
    Check 'Enable five-minute recovery rule'
    Write-Host 'Deployed event-driven dbt orchestration. LAUNCH REMAINS DISABLED until corrected image, AuthCheck and Redshift permissions pass.'
}
finally {
    Pop-Location
    if (Test-Path $Temp) { Remove-Item -LiteralPath $Temp -Recurse -Force }
}
