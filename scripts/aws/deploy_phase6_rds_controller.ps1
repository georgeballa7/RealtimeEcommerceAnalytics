<#
Guarded Phase 6 rollout. Builds shared Lambda, Shopify generator and 22:00 lifecycle images.
Default: build/push and Terraform plan, no apply. -Apply prompts for approval.
Projector, RDS, SQS and Glue definitions are unchanged; nightly lifecycle remains at 22:00.
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
    $afternoonTask = AwsValue -AwsArgs @('scheduler','get-schedule','--name','ecommerce-demo-phase5-afternoon','--query','Target.EcsParameters.TaskDefinitionArn')
    if ($afternoonTask -ne $generatorTask) { throw 'Morning/afternoon generator task definitions differ; reconcile before release' }
    $lifecycleCron = AwsValue -AwsArgs @('scheduler','get-schedule','--name','ecommerce-demo-phase5-lifecycle','--query','ScheduleExpression')
    if ($lifecycleCron -ne 'cron(0 22 * * ? *)') {
        throw "Expected reviewed 22:00 lifecycle schedule, found: $lifecycleCron"
    }
    $lifecycleTask = AwsValue -AwsArgs @('scheduler','get-schedule','--name','ecommerce-demo-phase5-lifecycle','--query','Target.EcsParameters.TaskDefinitionArn')
    $projectorTask = AwsValue -AwsArgs @('lambda','get-function-configuration','--function-name',$controller,'--query','Environment.Variables.PROJECTOR_TASK_DEFINITION')
    $generatorImage = TaskImage $generatorTask
    $lifecycleImage = TaskImage $lifecycleTask
    $projectorImage = TaskImage $projectorTask

    & python -m pytest -q
    Assert-Exit 'Regression tests'
    $registry = "$accountId.dkr.ecr.$region.amazonaws.com"
    $tag = "phase6-release-$sha"
    # Push using the existing Docker Desktop credential-helper workaround.
    $tempConfig = Join-Path ([IO.Path]::GetTempPath()) ("ecr-release-" + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tempConfig | Out-Null
    $token = & aws ecr get-login-password --profile $profileName --region $region
    Assert-Exit 'Get ECR token'
    $auth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("AWS:$token"))
    [IO.File]::WriteAllText((Join-Path $tempConfig 'config.json'), (@{auths = @{$registry = @{auth = $auth}}} | ConvertTo-Json -Depth 5 -Compress), (New-Object Text.UTF8Encoding($false)))
    Remove-Variable token,auth -ErrorAction SilentlyContinue

    function Build-ReleaseImage([string]$repository, [string]$dockerFile) {
        $imageTag = [string]::Format('{0}/{1}:{2}', $registry, $repository, $tag)
        $digestResult = & aws ecr list-images --repository-name $repository --filter tagStatus=TAGGED --query "imageIds[?imageTag=='$tag'].imageDigest | [0]" --profile $profileName --region $region --output text
        Assert-Exit 'Inspect immutable ECR image'
        $digest = ($digestResult | Out-String).Trim()
        if (-not $digest -or $digest -eq 'None') {
            & docker buildx build --platform linux/amd64 --provenance=false --sbom=false --load --file (Join-Path $root $dockerFile) --tag $imageTag $root | Out-Host
            Assert-Exit "Build $repository"
            & docker --config $tempConfig push $imageTag | Out-Host
            Assert-Exit "Push $repository"
            $digest = AwsValue -AwsArgs @('ecr','describe-images','--repository-name',$repository,'--image-ids',"imageTag=$tag",'--query','imageDetails[0].imageDigest')
        }
        if ($digest -notmatch '^sha256:[a-f0-9]{64}$') { throw "Invalid digest for $repository" }
        $media = AwsValue -AwsArgs @('ecr','describe-images','--repository-name',$repository,'--image-ids',"imageTag=$tag",'--query','imageDetails[0].imageManifestMediaType')
        if ($media -notin @('application/vnd.docker.distribution.manifest.v2+json','application/vnd.oci.image.manifest.v1+json')) { throw "Unsupported image type $media" }
        return [string]::Format('{0}/{1}@{2}', $registry,$repository,$digest)
    }
    $newImage = Build-ReleaseImage 'ecommerce-phase5-live-lambda-demo' 'Dockerfile.phase5-lambda'
    $newGeneratorImage = Build-ReleaseImage 'ecommerce-phase5-generator-demo' 'Dockerfile.phase5-generator'
    $newLifecycleImage = Build-ReleaseImage 'ecommerce-phase5-lifecycle-demo' 'Dockerfile.phase5-lifecycle'
    if ($newImage -isnot [string] -or $newGeneratorImage -isnot [string] -or $newLifecycleImage -isnot [string]) {
        throw 'Image helper returned non-scalar output; aborting before Terraform'
    }
    if ($newImage -notmatch '@sha256:[a-f0-9]{64} {
        throw 'Image helper returned an invalid immutable image URI'
    }
    Write-Host "Verified shared Lambda image: $newImage"
    Write-Host "Verified generator image: $newGeneratorImage"
    Write-Host "Verified 22:00 lifecycle image: $newLifecycleImage"
    Write-Host 'Existing projector image will be reused.'

    $vars = [ordered]@{
        aws_account_id = $accountId; aws_profile = $profileName; aws_region = $region
        shopify_shop_domain = $domain; shopify_client_credentials_secret_arn = $clientSecret
        shopify_webhook_secret_arn = $webhookSecret
        phase5_lambda_image = $newImage; phase5_projector_image = $projectorImage
        phase5_generator_image = $newGeneratorImage; phase5_lifecycle_image = $newLifecycleImage
        enable_redshift_warehouse = $true; enable_phase6_glue_silver = $true
        enable_phase6_glue_rds_lease = $true
        enable_phase5_legacy_lifecycle_schedule = $true
    }
    $tfvarsPath = Join-Path ([IO.Path]::GetTempPath()) ("phase6-controller-" + [guid]::NewGuid().ToString('N') + '.tfvars.json')
    [IO.File]::WriteAllText($tfvarsPath, ($vars | ConvertTo-Json -Depth 5 -Compress), (New-Object Text.UTF8Encoding($false)))
    Push-Location $terraformDir
    try {
        & terraform init -input=false
        Assert-Exit 'Terraform init'
        & terraform validate
        Assert-Exit 'Terraform validate'
        # -no-color preserves Terraform's rejected variable names and expected
        # types in redirected PowerShell output. Never print the tfvars JSON:
        # it contains secret ARNs and immutable image URIs.
        & terraform plan -no-color "-var-file=$tfvarsPath" "-out=$planPath"
        Assert-Exit 'Terraform plan'
        # Use Terraform's native saved-plan review. No custom JSON diff gate:
        # ECS task revisions naturally include provider-populated defaults.
        & terraform show $planPath
        Assert-Exit 'Show reviewed plan'
        Write-Host 'Review the Terraform plan above. No custom resource-diff gate is applied.'
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
    foreach ($schedule in @('morning','afternoon')) {
        $scheduledTask = AwsValue -AwsArgs @('scheduler','get-schedule','--name',"ecommerce-demo-phase5-$schedule",'--query','Target.EcsParameters.TaskDefinitionArn')
        $deployedGeneratorImage = TaskImage $scheduledTask
        if ($deployedGeneratorImage -ne $newGeneratorImage) { throw "Generator image not updated in $schedule schedule" }
    }
    $scheduledLifecycle = AwsValue -AwsArgs @('scheduler','get-schedule','--name','ecommerce-demo-phase5-lifecycle','--query','Target.EcsParameters.TaskDefinitionArn')
    if ((TaskImage $scheduledLifecycle) -ne $newLifecycleImage) {
        throw 'Scheduled 22:00 lifecycle image differs from reviewed release'
    }
    $glueJob = AwsValue -AwsArgs @('lambda','get-function-configuration','--function-name',$controller,'--query','Environment.Variables.PHASE6_GLUE_JOB_NAME')
    if ($glueJob -ne 'ecommerce-phase6-shopify-silver-demo') { throw 'Controller Glue environment not deployed' }
    $glueAction = AwsValue -AwsArgs @('iam','get-role-policy','--role-name','ecommerce-phase5-controller-demo','--policy-name','phase6-read-glue-status-for-safe-rds-stop','--query','PolicyDocument.Statement[0].Action[0]')
    if ($glueAction -ne 'glue:GetJobRun') { throw 'Controller Glue-read IAM not verified' }
    Write-Host 'Controller, generator and 22:00 lifecycle release verified; RDS and projector preserved.'
}
finally {
    Pop-Location
    if ($tfvarsPath) { Remove-Item -LiteralPath $tfvarsPath -Force -ErrorAction SilentlyContinue }
    if ($tempConfig) { Remove-Item -LiteralPath $tempConfig -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path $planPath) { Remove-Item -LiteralPath $planPath -Force -ErrorAction SilentlyContinue }
}
 -or $newGeneratorImage -notmatch '@sha256:[a-f0-9]{64} {
        throw 'Image helper returned an invalid immutable image URI'
    }
    Write-Host "Verified shared Lambda image: $newImage"
    Write-Host "Verified generator image: $newGeneratorImage"
    Write-Host 'Existing projector and lifecycle images will be reused.'

    $vars = [ordered]@{
        aws_account_id = $accountId; aws_profile = $profileName; aws_region = $region
        shopify_shop_domain = $domain; shopify_client_credentials_secret_arn = $clientSecret
        shopify_webhook_secret_arn = $webhookSecret
        phase5_lambda_image = $newImage; phase5_projector_image = $projectorImage
        phase5_generator_image = $newGeneratorImage; phase5_lifecycle_image = $lifecycleImage
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
        # -no-color preserves Terraform's rejected variable names and expected
        # types in redirected PowerShell output. Never print the tfvars JSON:
        # it contains secret ARNs and immutable image URIs.
        & terraform plan -no-color "-var-file=$tfvarsPath" "-out=$planPath"
        Assert-Exit 'Terraform plan'
        # Use Terraform's native saved-plan review. No custom JSON diff gate:
        # ECS task revisions naturally include provider-populated defaults.
        & terraform show $planPath
        Assert-Exit 'Show reviewed plan'
        Write-Host 'Review the Terraform plan above. No custom resource-diff gate is applied.'
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
    foreach ($schedule in @('morning','afternoon')) {
        $scheduledTask = AwsValue -AwsArgs @('scheduler','get-schedule','--name',"ecommerce-demo-phase5-$schedule",'--query','Target.EcsParameters.TaskDefinitionArn')
        $deployedGeneratorImage = TaskImage $scheduledTask
        if ($deployedGeneratorImage -ne $newGeneratorImage) { throw "Generator image not updated in $schedule schedule" }
    }
    $glueJob = AwsValue -AwsArgs @('lambda','get-function-configuration','--function-name',$controller,'--query','Environment.Variables.PHASE6_GLUE_JOB_NAME')
    if ($glueJob -ne 'ecommerce-phase6-shopify-silver-demo') { throw 'Controller Glue environment not deployed' }
    $glueAction = AwsValue -AwsArgs @('iam','get-role-policy','--role-name','ecommerce-phase5-controller-demo','--policy-name','phase6-read-glue-status-for-safe-rds-stop','--query','PolicyDocument.Statement[0].Action[0]')
    if ($glueAction -ne 'glue:GetJobRun') { throw 'Controller Glue-read IAM not verified' }
    Write-Host 'Controller and generator release verified. Original schedule times, RDS and projector/lifecycle unchanged.'
}
finally {
    Pop-Location
    if ($tfvarsPath) { Remove-Item -LiteralPath $tfvarsPath -Force -ErrorAction SilentlyContinue }
    if ($tempConfig) { Remove-Item -LiteralPath $tempConfig -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path $planPath) { Remove-Item -LiteralPath $planPath -Force -ErrorAction SilentlyContinue }
}
 -or $newLifecycleImage -notmatch '@sha256:[a-f0-9]{64} {
        throw 'Image helper returned an invalid immutable image URI'
    }
    Write-Host "Verified shared Lambda image: $newImage"
    Write-Host "Verified generator image: $newGeneratorImage"
    Write-Host 'Existing projector and lifecycle images will be reused.'

    $vars = [ordered]@{
        aws_account_id = $accountId; aws_profile = $profileName; aws_region = $region
        shopify_shop_domain = $domain; shopify_client_credentials_secret_arn = $clientSecret
        shopify_webhook_secret_arn = $webhookSecret
        phase5_lambda_image = $newImage; phase5_projector_image = $projectorImage
        phase5_generator_image = $newGeneratorImage; phase5_lifecycle_image = $lifecycleImage
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
        # -no-color preserves Terraform's rejected variable names and expected
        # types in redirected PowerShell output. Never print the tfvars JSON:
        # it contains secret ARNs and immutable image URIs.
        & terraform plan -no-color "-var-file=$tfvarsPath" "-out=$planPath"
        Assert-Exit 'Terraform plan'
        # Use Terraform's native saved-plan review. No custom JSON diff gate:
        # ECS task revisions naturally include provider-populated defaults.
        & terraform show $planPath
        Assert-Exit 'Show reviewed plan'
        Write-Host 'Review the Terraform plan above. No custom resource-diff gate is applied.'
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
    foreach ($schedule in @('morning','afternoon')) {
        $scheduledTask = AwsValue -AwsArgs @('scheduler','get-schedule','--name',"ecommerce-demo-phase5-$schedule",'--query','Target.EcsParameters.TaskDefinitionArn')
        $deployedGeneratorImage = TaskImage $scheduledTask
        if ($deployedGeneratorImage -ne $newGeneratorImage) { throw "Generator image not updated in $schedule schedule" }
    }
    $glueJob = AwsValue -AwsArgs @('lambda','get-function-configuration','--function-name',$controller,'--query','Environment.Variables.PHASE6_GLUE_JOB_NAME')
    if ($glueJob -ne 'ecommerce-phase6-shopify-silver-demo') { throw 'Controller Glue environment not deployed' }
    $glueAction = AwsValue -AwsArgs @('iam','get-role-policy','--role-name','ecommerce-phase5-controller-demo','--policy-name','phase6-read-glue-status-for-safe-rds-stop','--query','PolicyDocument.Statement[0].Action[0]')
    if ($glueAction -ne 'glue:GetJobRun') { throw 'Controller Glue-read IAM not verified' }
    Write-Host 'Controller and generator release verified. Original schedule times, RDS and projector/lifecycle unchanged.'
}
finally {
    Pop-Location
    if ($tfvarsPath) { Remove-Item -LiteralPath $tfvarsPath -Force -ErrorAction SilentlyContinue }
    if ($tempConfig) { Remove-Item -LiteralPath $tempConfig -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path $planPath) { Remove-Item -LiteralPath $planPath -Force -ErrorAction SilentlyContinue }
}
) {
        throw 'Image helper returned an invalid immutable image URI'
    }
    Write-Host "Verified shared Lambda image: $newImage"
    Write-Host "Verified generator image: $newGeneratorImage"
    Write-Host 'Existing projector and lifecycle images will be reused.'

    $vars = [ordered]@{
        aws_account_id = $accountId; aws_profile = $profileName; aws_region = $region
        shopify_shop_domain = $domain; shopify_client_credentials_secret_arn = $clientSecret
        shopify_webhook_secret_arn = $webhookSecret
        phase5_lambda_image = $newImage; phase5_projector_image = $projectorImage
        phase5_generator_image = $newGeneratorImage; phase5_lifecycle_image = $lifecycleImage
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
        # -no-color preserves Terraform's rejected variable names and expected
        # types in redirected PowerShell output. Never print the tfvars JSON:
        # it contains secret ARNs and immutable image URIs.
        & terraform plan -no-color "-var-file=$tfvarsPath" "-out=$planPath"
        Assert-Exit 'Terraform plan'
        # Use Terraform's native saved-plan review. No custom JSON diff gate:
        # ECS task revisions naturally include provider-populated defaults.
        & terraform show $planPath
        Assert-Exit 'Show reviewed plan'
        Write-Host 'Review the Terraform plan above. No custom resource-diff gate is applied.'
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
    foreach ($schedule in @('morning','afternoon')) {
        $scheduledTask = AwsValue -AwsArgs @('scheduler','get-schedule','--name',"ecommerce-demo-phase5-$schedule",'--query','Target.EcsParameters.TaskDefinitionArn')
        $deployedGeneratorImage = TaskImage $scheduledTask
        if ($deployedGeneratorImage -ne $newGeneratorImage) { throw "Generator image not updated in $schedule schedule" }
    }
    $glueJob = AwsValue -AwsArgs @('lambda','get-function-configuration','--function-name',$controller,'--query','Environment.Variables.PHASE6_GLUE_JOB_NAME')
    if ($glueJob -ne 'ecommerce-phase6-shopify-silver-demo') { throw 'Controller Glue environment not deployed' }
    $glueAction = AwsValue -AwsArgs @('iam','get-role-policy','--role-name','ecommerce-phase5-controller-demo','--policy-name','phase6-read-glue-status-for-safe-rds-stop','--query','PolicyDocument.Statement[0].Action[0]')
    if ($glueAction -ne 'glue:GetJobRun') { throw 'Controller Glue-read IAM not verified' }
    Write-Host 'Controller and generator release verified. Original schedule times, RDS and projector/lifecycle unchanged.'
}
finally {
    Pop-Location
    if ($tfvarsPath) { Remove-Item -LiteralPath $tfvarsPath -Force -ErrorAction SilentlyContinue }
    if ($tempConfig) { Remove-Item -LiteralPath $tempConfig -Recurse -Force -ErrorAction SilentlyContinue }
    if (Test-Path $planPath) { Remove-Item -LiteralPath $planPath -Force -ErrorAction SilentlyContinue }
}
