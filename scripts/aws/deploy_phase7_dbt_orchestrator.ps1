<#
Read-only verification of the Phase 7 event-driven dbt orchestrator.
It was deployed into AWS CloudFormation stack
ecommerce-phase7-dbt-orchestrator-demo; never create a second unmanaged Lambda.
Use the connected AWS stack for subsequent source revisions and controlled
activation only after the new dbt image, ECS AuthCheck and SQL grants succeed.
#>
[CmdletBinding()]
param([switch]$Apply)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Region = 'eu-central-1'
$Profile = 'ecommerce-admin'
$Account = '825414820081'
$Name = 'ecommerce-phase7-dbt-orchestrator-demo'
$Arn = "arn:aws:lambda:$($Region):$($Account):function:$Name"
$Rules = @('ecommerce-phase7-loader-success-dbt-demo',
           'ecommerce-phase7-dbt-task-stopped-demo',
           'ecommerce-phase7-dbt-reconcile-demo')
function Check([string]$Label) {
    if ($LASTEXITCODE -ne 0) { throw "$Label failed (exit code $LASTEXITCODE)" }
}
if ($Apply) {
    throw 'Already deployed via CloudFormation. Refusing a second direct Lambda deployment.'
}
Push-Location $Root
try {
    & python -X utf8 -c 'import ast,sys;ast.parse(open(sys.argv[1]).read())' (Join-Path $Root 'scripts\aws\phase7_dbt_orchestrator.py')
    Check 'Python syntax'
    $Actual = (& aws sts get-caller-identity --region $Region --profile $Profile --query Account --output text | Out-String).Trim()
    Check 'AWS identity'
    if ($Actual -ne $Account) { throw 'Wrong AWS account' }
    $StackStatus = (& aws cloudformation describe-stacks --stack-name $Name --region $Region --profile $Profile --query 'Stacks[0].StackStatus' --output text | Out-String).Trim()
    Check 'Orchestrator CloudFormation stack'
    if ($StackStatus -ne 'CREATE_COMPLETE' -and $StackStatus -ne 'UPDATE_COMPLETE') {
        throw "Orchestrator CloudFormation status unexpected: $StackStatus"
    }
    $CfgJson = & aws lambda get-function-configuration --function-name $Name --region $Region --profile $Profile --output json
    Check 'Lambda configuration'
    $Cfg = $CfgJson | Out-String | ConvertFrom-Json
    if ($Cfg.State -ne 'Active' -or $Cfg.Handler -ne 'index.handler' -or
        $Cfg.Environment.Variables.DBT_LAUNCH_ENABLED -ne 'false') {
        throw 'Unexpected orchestrator Lambda code, state or launch flag'
    }
    foreach ($Rule in $Rules) {
        $TargetsJson = & aws events list-targets-by-rule --rule $Rule --region $Region --profile $Profile --output json
        Check "EventBridge targets: $Rule"
        $Targets = ($TargetsJson | Out-String | ConvertFrom-Json).Targets
        if (@($Targets).Count -ne 1 -or $Targets[0].Arn -ne $Arn) {
            throw "Unexpected EventBridge targets for $Rule"
        }
    }
    $RuleState = (& aws events describe-rule --name 'ecommerce-phase7-dbt-reconcile-demo' --region $Region --profile $Profile --query State --output text | Out-String).Trim()
    Check 'Recovery rule state'
    if ($RuleState -ne 'ENABLED') { throw 'Five-minute recovery rule is disabled' }
    $Destination = (& aws lambda get-function-event-invoke-config --function-name 'ecommerce-phase6-silver-redshift-loader-demo' --region $Region --profile $Profile --query 'DestinationConfig.OnSuccess.Destination' --output text | Out-String).Trim()
    Check 'Loader completion destination'
    if ($Destination -ne "arn:aws:events:$($Region):$($Account):event-bus/default") {
        throw 'Loader OnSuccess destination differs'
    }
    Write-Host 'VERIFIED: CloudFormation dbt orchestrator, three EventBridge rule targets, loader-success destination, recovery schedule.'
    Write-Host 'DBT LAUNCH IS DISABLED until the fixed image, AuthCheck and SQL grants pass.'
}
finally { Pop-Location }
