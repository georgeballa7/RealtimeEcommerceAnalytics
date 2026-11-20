<# 
.SYNOPSIS
    Validate or deploy the reviewed Phase 6 Shopify Silver -> Redshift loader.
.DESCRIPTION
    Default is validation ONLY. -Apply updates the existing Lambda ZIP after
    validating Python, artifact SHA-256, exact AWS account, handler, runtime,
    role and DynamoDB acknowledgement permissions. Does not run any pipeline.
#>
[CmdletBinding()]
param([switch]$Apply)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Region = 'eu-central-1'
$Profile = 'ecommerce-admin'
$Account = '825414820081'
$Function = 'ecommerce-phase6-silver-redshift-loader-demo'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Source = Join-Path $Root 'scripts\redshift\load_shopify_silver.py'
$Temp = Join-Path ([IO.Path]::GetTempPath()) ('phase6-loader-' + [guid]::NewGuid().ToString('N'))

try {
    if (-not (Test-Path $Source -PathType Leaf)) { throw "Missing loader source: $Source" }
    $sourceText = [IO.File]::ReadAllText($Source)
    if (-not $sourceText.Contains('def acknowledge_verified_slot(') -or
        -not $sourceText.Contains('def lambda_handler(') -or
        -not $sourceText.Contains('process_published_snapshots(')) {
        throw 'Loader missing verified manifest or durable acknowledgement contract'
    }
    & python -X utf8 -c 'import ast,sys;ast.parse(open(sys.argv[1],encoding="utf-8").read())' $Source
    if ($LASTEXITCODE -ne 0) { throw 'Loader Python AST validation failed' }

    New-Item -ItemType Directory -Path $Temp -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination (Join-Path $Temp 'index.py')
    $Zip = Join-Path $Temp 'loader.zip'
    Compress-Archive -LiteralPath (Join-Path $Temp 'index.py') -DestinationPath $Zip
    $Verify = Join-Path $Temp 'verify'
    Expand-Archive -LiteralPath $Zip -DestinationPath $Verify
    $Unzipped = Join-Path $Verify 'index.py'
    if (-not (Test-Path $Unzipped) -or
        (Get-FileHash $Unzipped -Algorithm SHA256).Hash -ne
        (Get-FileHash $Source -Algorithm SHA256).Hash) {
        throw 'ZIP does not contain identical index.py source'
    }
    $Hasher = [Security.Cryptography.SHA256]::Create()
    try { $ExpectedCodeHash = [Convert]::ToBase64String($Hasher.ComputeHash([IO.File]::ReadAllBytes($Zip))) }
    finally { $Hasher.Dispose() }
    Write-Host 'Validated Phase 6 Silver loader ZIP; code SHA-256:' $ExpectedCodeHash
    if (-not $Apply) {
        Write-Host 'VALIDATE ONLY: no AWS calls or changes. Pass -Apply after Terraform IAM plan/apply review.'
        return
    }

    $CurrentAccount = (& aws sts get-caller-identity --profile $Profile --region $Region --query Account --output text | Out-String).Trim()
    if ($LASTEXITCODE -ne 0 -or $CurrentAccount -ne $Account) { throw 'Wrong AWS account' }
    $Cfg = (& aws lambda get-function-configuration --function-name $Function --profile $Profile --region $Region --output json | Out-String | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0 -or $Cfg.PackageType -ne 'Zip' -or
        $Cfg.Runtime -ne 'python3.12' -or $Cfg.Handler -ne 'index.lambda_handler' -or
        $Cfg.Role -ne "arn:aws:iam::$Account:role/ecommerce-phase6-redshift-loader-demo") {
        throw 'Unexpected loader Lambda configuration; refused'
    }
    $ControllerArn = "arn:aws:dynamodb:${Region}:${Account}:table/ecommerce-demo-phase5-controller"
    $Simulation = (& aws iam simulate-principal-policy --policy-source-arn $Cfg.Role --action-names dynamodb:GetItem dynamodb:UpdateItem --resource-arns $ControllerArn --profile $Profile --region $Region --output json | Out-String | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0 -or @($Simulation.EvaluationResults).Count -ne 2 -or
        @($Simulation.EvaluationResults | Where-Object { $_.EvalDecision -ne 'allowed' }).Count -ne 0) {
        throw 'Loader IAM lacks verified slot acknowledgement permissions; apply reviewed Terraform first'
    }
    $Uri = 'fileb://' + ($Zip -replace '\\', '/')
    $Upload = (& aws lambda update-function-code --function-name $Function --zip-file $Uri --profile $Profile --region $Region --output json | Out-String | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0 -or $Upload.CodeSha256 -ne $ExpectedCodeHash) {
        throw 'Loader upload failed or returned unexpected SHA-256'
    }
    & aws lambda wait function-updated --function-name $Function --profile $Profile --region $Region
    if ($LASTEXITCODE -ne 0) { throw 'Loader update did not reach stable state' }
    $Final = (& aws lambda get-function-configuration --function-name $Function --profile $Profile --region $Region --output json | Out-String | ConvertFrom-Json)
    if ($LASTEXITCODE -ne 0 -or $Final.CodeSha256 -ne $ExpectedCodeHash -or
        $Final.LastUpdateStatus -ne 'Successful') {
        throw 'Loader deployment verification FAILED'
    }
    Write-Host 'Loader deployed with verified ZIP digest; no Lambda invoked.'
}
finally {
    if (Test-Path $Temp) { Remove-Item -LiteralPath $Temp -Recurse -Force }
}
