<#
Guarded Phase 6 RDS-controller rollout. Builds ONLY the shared Lambda image.
Default: build/push and Terraform plan, no apply. -Apply prompts for approval.
The generator, projector, lifecycle, RDS, SQS, Glue, and schedules are unchanged.
#>
[CmdletBinding()]
param([switch]$Apply)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$profileName = 'ecommerce-admin'
$region = 'eu-central-1'
$accountId = '825414820081'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$terraformDir = Join-Path $root 'infrastructure\terraform'
$tempConfig = $null
$tfvarsPath = $null
$planPath = Join-Path $terraformDir 'phase6-controller-reviewed.tfplan'

function Assert-Exit([string]$step) {
    if ($LASTEXITCODE -ne 0) { throw "$step failed ($LASTEXITCODE)" }
}
function AwsValue([string[]]$AwsArgs) {
    $out = & aws @AwsArgs --profile $profileName --region $region --output text
    Assert-Exit 'AWS CLI'
    $v = ($out | Out-String).Trim()
    if (-not $v -or $v -eq 'None') { throw "Required AWS value missing: $($AwsArgs -join ' ')" }
    return $v
}
function TaskImage([string]$task) {
    return AwsValue -AwsArgs @('ecs','describe-task-definition','--task-definition',$task,'--query','taskDefinition.containerDefinitions[0].image')
}

Push-Location $root
try {
    $branch = (& git branch --show-current).Trim()
    Assert-Exit 'Git branch'
    if ($branch -ne 'feature/redshift-warehouse-dbt-churn') { throw "Incorrect branch: $branch" }
    $dirty = @(& git status --porcelain)
    Assert-Exit 'Git worktree'
    if ($dirty.Count -gt 0) { throw 'Git working tree must be clean' }
    $sha = (& git rev-parse --short=12 HEAD).Trim()
    Assert-Exit 'Git SHA'
    $account = AwsValue -AwsArgs @('sts','get-caller-identity','--query','Account')
    if ($account -ne $accountId) { throw "Wrong account: $account" }

    $controller = 'ecommerce-demo-phase5-controller'
    $api = 'ecommerce-demo-phase5-fastapi'
    $landing = 'ecommerce-demo-phase5-landing'
    $imageController = AwsValue -AwsArgs @('lambda','get-function','--function-name',$controller,'--query','Code.ImageUri')
    $imageApi = AwsValue -AwsArgs @('lambda','get-function','--function-name',$api,'--query','Code.ImageUri')
    $imageLanding = AwsValue -AwsArgs @('lambda','get-function','--function-name',$landing,'--query','Code.ImageUri')
    if ($imageController -ne $imageApi -or $imageApi -ne $imageLanding) { throw 'Shared Lambda images drifted; reconcile first' }

    $domain = AwsValue -AwsArgs @('lambda','get-function-configuration','--function-name',$api,'--query','Environment.Variables.SHOPIFY_SHOP_DOMAIN')
    $webhookSecret = AwsValue -AwsArgs @('lambda','get-function-configuration','--function-name',$api,'--query','Environment.Variables.SHOPIFY_WEBHOOK_SECRET_ARN')
    $clientSecret = AwsValue -AwsArgs @('secretsmanager','describe-secret','--secret-id','ecommerce/demo/shopify/client-credentials','--query','ARN')
    $generatorTask = AwsValue -AwsArgs @('scheduler','get-schedule','--name','ecommerce-demo-phase5-morning','--query','Target.EcsParameters.TaskDefinitionArn')
    $lifecycleTask = AwsValue -AwsArgs @('scheduler','get-schedule','--name','ecommerce-demo-phase5-lifecycle','--query','Target.EcsParameters.TaskDefinitionArn')
    $projectorTask = AwsValue -AwsArgs @('lambda','get-function-configuration','--function-name',$controller,'--query','Environment.Variables.PROJECTOR_TASK_DEFINITION')
    $generatorImage = TaskImage $generatorTask
    $lifecycleImage = TaskImage $lifecycleTask
    $projectorImage = TaskImage $projectorTask

    & python -m pytest -q
    Assert-Exit 'Regression tests'
    $registry = "$accountId.dkr.ecr.$region.amazonaws.com"
    $repository = 'ecommerce-phase5-live-lambda-demo'
    $tag = "phase6-lease-$sha"
    $imageTag = [string]::Format('{0}/{1}:{2}', $registry, $repository, $tag)
    $digestResult = & aws ecr list-images --repository-name $repository --filter tagStatus=TAGGED --query "imageIds[?imageTag=='$tag'].imageDigest | [0]" --profile $profileName --region $region --output text
    Assert-Exit 'Inspect existing ECR image'
    $digest = ($digestResult | Out-String).Trim()
    if (-not $digest -or $digest -eq 'None') {
        & docker buildx build --platform linux/amd64 --provenance=false --sbom=false --load --file (Join-Path $root 'Dockerfile.phase5-lambda') --tag $imageTag $root
        Assert-Exit 'Build immutable Lambda image'
        $tempConfig = Join-Path ([IO.Path]::GetTempPath()) ("ecr-controller-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $tempConfig | Out-Null
        $token = & aws ecr get-login-password --profile $profileName --region $region
        Assert-Exit 'Get ECR token'
        $auth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("AWS:$token"))
        [IO.File]::WriteAllText((Join-Path $tempConfig 'config.json'), (@{auths = @{$registry = @{auth = $auth}}} | ConvertTo-Json -Depth 5 -Compress), (New-Object Text.UTF8Encoding($false)))
        Remove-Variable token,auth -ErrorAction SilentlyContinue
        & docker --config $tempConfig push $imageTag
        Assert-Exit 'Push Lambda image to ECR'
        $digest = AwsValue -AwsArgs @('ecr','describe-images','--repository-name',$repository,'--image-ids',"imageTag=$tag",'--query','imageDetails[0].imageDigest')
    }
    if ($digest -notmatch '^sha256:[a-f0-9]{64}$') { throw 'ECR digest invalid' }
    $media = AwsValue -AwsArgs @('ecr','describe-images','--repository-name',$repository,'--image-ids',"imageTag=$tag",'--query','imageDetails[0].imageManifestMediaType')
    if ($media -notin @('application/vnd.docker.distribution.manifest.v2+json','application/vnd.oci.image.manifest.v1+json')) { throw "Unsupported image type $media" }
    $newImage = [string]::Format('{0}/{1}@{2}', $registry,$repository,$digest)
    Write-Host "Verified new shared Lambda image: $newImage"

    $vars = [ordered]@{
        aws_account_id = $accountId; aws_profile = $profileName; aws_region = $region
        shopify_shop_domain = $domain; shopify_client_credentials_secret_arn = $clientSecret
        shopify_webhook_secret_arn = $webhookSecret
        phase5_lambda_image = $newImage; phase5_projector_image = $projectorImage
        phase5_generator_image = $generatorImage; phase5_lifecycle_image = $lifecycleImage
        enable_redshift_warehouse = $true; enable_phase6_glue_silver = $true
        enable_phase6_glue_rds_lease = $true
    }
    $tfvarsPath = Join-Path ([IO.Path]::GetTempPath()) ("phase6-controller-" + [guid]::NewGuid().ToString('N') + '.tfvars.json')
    [IO.File]::WriteAllText($tfvarsPath, ($vars | ConvertTo-Json -Depth 5 -Compress), (New-Object Text.UTF8Encoding($false)))
    Push-Location $terraformDir
    try {
        & terraform init -input=false
        Assert-Exit 'Terraform init'
        & terraform validate
        Assert-Exit 'Terraform validate'
        & terraform plan "-var-file=$tfvarsPath" "-out=$planPath"
        Assert-Exit 'Terraform plan'
        $plan = ((& terraform show -json $planPath | Out-String) | ConvertFrom-Json)
        Assert-Exit 'Terraform JSON'
        $allowed = @('aws_lambda_function.phase5_fastapi[0]','aws_lambda_function.phase5_landing[0]','aws_lambda_function.phase5_controller[0]','aws_iam_role_policy.phase6_controller_glue_inspect[0]')
        $changes = @($plan.resource_changes | Where-Object { ($_.change.actions -join ',') -ne 'no-op' })
        foreach ($change in $changes) {
            $address = [string]$change.address
            $actions = ($change.change.actions -join ',')
            Write-Host "$address : $actions"
            if ($address -notin $allowed) { throw "UNSAFE unexpected Terraform resource: $address" }
            if ($address -like 'aws_lambda_function.*') {
                if ($actions -ne 'update') { throw "UNSAFE Lambda replacement: $address" }
                $old = $change.change.before; $new = $change.change.after
                if ($new.image_uri -ne $newImage) { throw "UNSAFE image target: $address" }
                $allowedFields = if ($address -eq 'aws_lambda_function.phase5_controller[0]') { @('image_uri','environment') } else { @('image_uri') }
                foreach ($field in $old.PSObject.Properties.Name) {
                    if ($field -in $allowedFields) { continue }
                    if (($old.$field | ConvertTo-Json -Depth 15 -Compress) -cne ($new.$field | ConvertTo-Json -Depth 15 -Compress)) {
                        throw "UNSAFE attribute changed in $address : $field"
                    }
                }
                if ($address -eq 'aws_lambda_function.phase5_controller[0]') {
                    if ($new.environment[0].variables.PHASE6_GLUE_JOB_NAME -ne 'ecommerce-phase6-shopify-silver-demo') { throw 'Expected Glue job missing from controller environment' }
                }
            } elseif ($actions -ne 'create') { throw "UNSAFE IAM operation: $actions" }
        }
        if ($changes.Count -ne 4) { throw "Expected only 3 Lambda image updates + 1 Glue-read IAM policy, got $($changes.Count)" }
        & terraform show $planPath
        Assert-Exit 'Show reviewed plan'
        Write-Host 'Safety checks passed: no generator, scheduler, RDS or other infrastructure changes.'
        if (-not $Apply) {
            Write-Host 'PLAN ONLY. Re-run with -Apply to review and approve deployment.'
            return
        }
        $approval = Read-Host 'Type DEPLOY-GLUE-LEASE to apply this exact plan'
        if ($approval -cne 'DEPLOY-GLUE-LEASE') { throw 'Deployment not approved' }
        & terraform apply -input=false $planPath
        Assert-Exit 'Terraform apply'
    } finally { Pop-Location }

    foreach ($fn in @($controller,$api,$landing)) {
        & aws lambda wait function-updated --function-name $fn --profile $profileName --region $region
        Assert-Exit "Wait for Lambda $fn"
        $actual = AwsValue -AwsArgs @('lambda','get-function','--function-name',$fn,'--query','Code.ImageUri')
        if ($actual -ne $newImage) { throw "Deployed Lambda image mismatch: $fn" }
    }
    $glueJob = AwsValue -AwsArgs @('lambda','get-function-configuration','--function-name',$controller,'--query','Environment.Variables.PHASE6_GLUE_JOB_NAME')
    if ($glueJob -ne 'ecommerce-phase6-shopify-silver-demo') { throw 'Controller Glue environment not deployed' }
    $glueAction = AwsValue -AwsArgs @('iam','get-role-policy','--role-name','ecommerce-phase5-controller-demo','--policy-name','phase6-read-glue-status-for-safe-rds-stop','--query','PolicyDocument.Statement[0].Action[0]')
    if ($glueAction -ne 'glue:GetJobRun') { throw 'Controller Glue-read IAM not verified' }
    Write-Host 'Controller deployed and verified. Generator, RDS, schedules unchanged.'
}
finally {
    Pop-Location
    if ($tfvarsPath) { Remove-Item -LiteralPath $tfvarsPath -Force -ErrorAction SilentlyContinue }
    if ($tempConfig) { Remove-Item -LiteralPath $tempConfig -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path $planPath) { Remove-Item -LiteralPath $planPath -Force -ErrorAction SilentlyContinue }
}
