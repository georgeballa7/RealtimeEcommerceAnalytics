<#
.SYNOPSIS
    Prepare or deploy the opt-in Phase 6 dbt ECS task via reviewed Terraform.
.DESCRIPTION
    Reuses the existing running Lambda, generator, projector and lifecycle
    immutable images to preserve Phase 5 Terraform state. Default is PLAN ONLY.
    First apply without -PinnedImage creates the dbt foundation (ECR, IAM,
    CloudWatch, private SG rules). Publish the image separately, then supply
    its immutable @sha256 digest for the second reviewed apply.
    Does NOT launch dbt, Glue, Shopify generation, or an RDS database.
.EXAMPLE
    .\scripts\aws\deploy_phase6_dbt.ps1
.EXAMPLE
    .\scripts\aws\deploy_phase6_dbt.ps1 -Apply
.EXAMPLE
    .\scripts\aws\deploy_phase6_dbt.ps1 -PinnedImage '825414820081.dkr.ecr.eu-central-1.amazonaws.com/ecommerce-phase6-dbt-demo@sha256:...' -Apply
#>
[CmdletBinding()]
param(
    [switch]$Apply,
    [string]$PinnedImage = ''
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$AwsProfile = 'ecommerce-admin'
$Region = 'eu-central-1'
$AccountId = '825414820081'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$TerraformDir = Join-Path $Root 'infrastructure\terraform'
$PlanPath = Join-Path $TerraformDir 'phase6-dbt-reviewed.tfplan'
$PlanJson = Join-Path ([IO.Path]::GetTempPath()) ('dbt-plan-' + [guid]::NewGuid().ToString('N') + '.json')
$Guard = Join-Path $Root 'scripts\aws\phase7_dbt_plan_guard.py'
$VarsBuilder = Join-Path $Root 'scripts\aws\phase7_dbt_release_vars.py'
$StateJson = Join-Path ([IO.Path]::GetTempPath()) ('dbt-state-' + [guid]::NewGuid().ToString('N') + '.json')
$VarsFile = $null

function Assert-Exit([string]$Label) {
    if ($LASTEXITCODE -ne 0) { throw "$Label failed ($LASTEXITCODE)" }
}
function AwsValue([string[]]$Params) {
    $result = & aws @Params --profile $AwsProfile --region $Region --output text
    Assert-Exit 'AWS CLI'
    $s = ($result | Out-String).Trim()
    if (-not $s -or $s -eq 'None') {
        throw "Missing required live AWS value: $($Params[0]) $($Params[1])"
    }
    return $s
}
function ImageForTask([string]$TaskDefinition) {
    return AwsValue -Params @('ecs','describe-task-definition','--task-definition',$TaskDefinition,'--query','taskDefinition.containerDefinitions[0].image')
}

Push-Location $Root
try {
    $branch = (& git branch --show-current | Out-String).Trim()
    Assert-Exit 'Git branch'
    if ($branch -ne 'feature/dbt-analytics-churn-model') { throw "Unexpected working branch: $branch" }
    $changes = @(& git status --porcelain)
    Assert-Exit 'Git worktree'
    if ($changes.Count -gt 0) { throw 'Commit or stash working-tree changes first' }
    $account = AwsValue -Params @('sts','get-caller-identity','--query','Account')
    if ($account -ne $AccountId) { throw "Incorrect AWS account: $account" }

    if ($PinnedImage -ne '' -and $PinnedImage -notmatch '^[0-9]{12}[.]dkr[.]ecr[.]eu-central-1[.]amazonaws[.]com/ecommerce-phase6-dbt-demo@sha256:[a-f0-9]{64}$') {
        throw 'PinnedImage must be the immutable digest from ecommerce-phase6-dbt-demo in eu-central-1'
    }

    $LambdaName = 'ecommerce-demo-phase5-controller'
    $ApiName = 'ecommerce-demo-phase5-fastapi'
    $LandingName = 'ecommerce-demo-phase5-landing'
    $LambdaImage = AwsValue -Params @('lambda','get-function','--function-name',$LambdaName,'--query','Code.ImageUri')
    foreach ($name in @($ApiName,$LandingName)) {
        $other = AwsValue -Params @('lambda','get-function','--function-name',$name,'--query','Code.ImageUri')
        if ($other -ne $LambdaImage) { throw "Shared Phase5 Lambda image differs: $name" }
    }
    $ShopDomain = AwsValue -Params @('lambda','get-function-configuration','--function-name',$ApiName,'--query','Environment.Variables.SHOPIFY_SHOP_DOMAIN')
    $WebhookSecret = AwsValue -Params @('lambda','get-function-configuration','--function-name',$ApiName,'--query','Environment.Variables.SHOPIFY_WEBHOOK_SECRET_ARN')
    $ClientSecret = AwsValue -Params @('secretsmanager','describe-secret','--secret-id','ecommerce/demo/shopify/client-credentials','--query','ARN')
    $MorningTask = AwsValue -Params @('scheduler','get-schedule','--name','ecommerce-demo-phase5-morning','--query','Target.EcsParameters.TaskDefinitionArn')
    $AfternoonTask = AwsValue -Params @('scheduler','get-schedule','--name','ecommerce-demo-phase5-afternoon','--query','Target.EcsParameters.TaskDefinitionArn')
    if ($MorningTask -ne $AfternoonTask) { throw 'Generator task definitions differ' }
    $GeneratorImage = ImageForTask $MorningTask
    $ProjectorTask = AwsValue -Params @('lambda','get-function-configuration','--function-name',$LambdaName,'--query','Environment.Variables.PROJECTOR_TASK_DEFINITION')
    $ProjectorImage = ImageForTask $ProjectorTask
    $LifecycleTask = AwsValue -Params @('scheduler','get-schedule','--name','ecommerce-demo-phase5-lifecycle','--query','Target.EcsParameters.TaskDefinitionArn')
    $LifecycleImage = ImageForTask $LifecycleTask

    foreach ($image in @($LambdaImage,$GeneratorImage,$ProjectorImage,$LifecycleImage)) {
        if ($image -notmatch '@sha256:[a-f0-9]{64}$') {
            throw 'Refusing to plan with mutable or unpinned deployed image'
        }
    }
    $stage = if ($PinnedImage) { 'task' } else { 'foundation' }
    # Temporary inputs are reconstructed from the *actual* Terraform state
    # after init. Never assume omitted optional image variables are disabled.
    $VarsFile = Join-Path ([IO.Path]::GetTempPath()) ("dbt-vars-" + [guid]::NewGuid().ToString('N') + '.tfvars.json')

    Push-Location $TerraformDir
    try {
        & terraform init -input=false
        Assert-Exit 'Terraform init'
        & terraform show -json | Out-File -LiteralPath $StateJson -Encoding utf8
        Assert-Exit 'Read current Terraform state JSON'
        # Native Windows PowerShell can drop an empty final argument.
        # During foundation stage, omit --pinned-image entirely and let
        # argparse use its safe empty-string default.
        $builderArgs = @('-X', 'utf8', $VarsBuilder, '--state', $StateJson, '--output', $VarsFile, '--account', $AccountId)
        if ($PinnedImage -ne '') {
            $builderArgs += @('--pinned-image', $PinnedImage)
        }
        & python @builderArgs
        Assert-Exit 'Preserve existing Terraform resources and digest-pinned images'
        $saved = Get-Content -LiteralPath $VarsFile -Raw | ConvertFrom-Json
        foreach ($match in @(
            @($saved.phase5_lambda_image, $LambdaImage),
            @($saved.phase5_generator_image, $GeneratorImage),
            @($saved.phase5_projector_image, $ProjectorImage),
            @($saved.phase5_lifecycle_image, $LifecycleImage),
            @($saved.shopify_shop_domain, $ShopDomain),
            @($saved.shopify_webhook_secret_arn, $WebhookSecret),
            @($saved.shopify_client_credentials_secret_arn, $ClientSecret)
        )) {
            if ($match[0] -ne $match[1]) {
                throw 'Live AWS deployment differs from Terraform state. Stop and reconcile drift.'
            }
        }
        & terraform validate
        Assert-Exit 'Terraform validate'
        & terraform plan -no-color "-var-file=$VarsFile" "-out=$PlanPath"
        Assert-Exit 'Terraform plan'
        & terraform show -json $PlanPath | Out-File -LiteralPath $PlanJson -Encoding utf8
        Assert-Exit 'Terraform show JSON'
        & python -X utf8 $Guard --plan $PlanJson --stage $stage
        Assert-Exit 'Strict dbt-only Terraform resource review'
        & terraform show $PlanPath
        Assert-Exit 'Terraform show'
        if (-not $Apply) {
            Write-Host 'PLAN ONLY. Review the exact changes, then rerun with -Apply.'
            return
        }
        $approval = Read-Host 'Type DEPLOY-DBT to apply this exact Terraform plan'
        if ($approval -cne 'DEPLOY-DBT') { throw 'Apply not approved' }
        & terraform apply -input=false $PlanPath
        Assert-Exit 'Terraform apply'
    }
    finally { Pop-Location }

    Write-Host 'dbt Terraform release applied. No dbt task was launched.'
    Write-Host 'Run terraform output -json for ECR, task-definition and security-group identifiers.'
}
finally {
    Pop-Location
    if ($VarsFile) { Remove-Item -LiteralPath $VarsFile -Force -ErrorAction SilentlyContinue }
    if (Test-Path $PlanJson) { Remove-Item -LiteralPath $PlanJson -Force -ErrorAction SilentlyContinue }
    if (Test-Path $StateJson) { Remove-Item -LiteralPath $StateJson -Force -ErrorAction SilentlyContinue }
    if (Test-Path $PlanPath) { Remove-Item -LiteralPath $PlanPath -Force -ErrorAction SilentlyContinue }
}
