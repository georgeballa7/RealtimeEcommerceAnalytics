<#
.SYNOPSIS
  One-command Phase 5 deployment: tests -> ECR -> plan/apply -> SQL migration
  -> readiness -> Shopify subscriptions.
.DESCRIPTION
  Uses the project feature branch, its existing Shopify app credentials, one
  Terraform apply, and idempotent ECR image digests. Deployment is interactive
  at the Terraform plan review; no second infrastructure apply is required.
  The two Europe/Berlin generator slots, SQS Lambda trigger, and controller
  minute reconciliation are enabled in that single apply.
.EXAMPLE
  .\scripts\aws\deploy_phase5_once.ps1
#>
[CmdletBinding()]
param(
    [string]$AwsProfile = 'ecommerce-admin',
    [string]$Region = 'eu-central-1',
    [string]$Environment = 'demo',
    [string]$SecretId = 'ecommerce/demo/shopify/client-credentials',
    [string]$ShopDomain = 'realtime-ecommerce-analytics.myshopify.com',
    [switch]$ApprovePlan
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
function Assert-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit $LASTEXITCODE). Deployment stopped." }
}
Push-Location $root
try {
    $branch = (& git branch --show-current).Trim()
    Assert-Exit 'Git branch detection'
    if ($branch -ne 'feature/shopify-live-event-generator') {
        throw "Switch to feature/shopify-live-event-generator (found: $branch)"
    }
    $dirty = & git status --porcelain
    Assert-Exit 'Git working tree'
    if ($dirty) { throw 'Working tree is not clean. Commit/stash your local edits before deployment.' }
    $account = (& aws sts get-caller-identity --profile $AwsProfile --query Account --output text).Trim()
    Assert-Exit 'AWS identity check'
    if ($account -ne '825414820081') { throw "Unexpected AWS account: $account" }

    Write-Host '=== Phase 5: full repository tests ==='
    & python -m pip install -e '.[api,aws,postgres,shopify,dev]'
    Assert-Exit 'Dependency installation'
    & python -m pytest -q
    Assert-Exit 'Full Python test suite'

    Push-Location (Join-Path $root 'infrastructure/terraform')
    try {
        # Read-only formatting check: never mutate tracked Terraform files.
        # Validation is authoritative; whitespace differences do not require a new rollout.
        & terraform fmt -check -diff
        if ($LASTEXITCODE -ne 0) {
            Write-Warning 'Terraform files need formatting, but deployment will not modify source files. Continuing to terraform validate.'
        }
        & terraform init
        Assert-Exit 'Terraform initialization'
        & terraform validate
        Assert-Exit 'Terraform validation'
        $tfAddresses = @(& terraform state list)
        Assert-Exit 'Terraform state listing'

        $registry = "$account.dkr.ecr.$Region.amazonaws.com"
        $repositories = [ordered]@{
            lambda = 'ecommerce-phase5-live-lambda-' + $Environment
            projector = 'ecommerce-phase5-projector-' + $Environment
            generator = 'ecommerce-phase5-generator-' + $Environment
            lifecycle = 'ecommerce-phase5-lifecycle-' + $Environment
        }
        foreach ($kind in $repositories.Keys) {
            $name = $repositories[$kind]
            $address = 'aws_ecr_repository.phase5_' + $kind
            $existingRepo = (& aws ecr describe-repositories --region $Region --profile $AwsProfile --query "repositories[?repositoryName=='$name'].repositoryName | [0]" --output text).Trim()
            Assert-Exit "Inspect ECR repository $name"
            if (-not $existingRepo -or $existingRepo -eq 'None') {
                & aws ecr create-repository --repository-name $name --image-tag-mutability IMMUTABLE --image-scanning-configuration scanOnPush=true --region $Region --profile $AwsProfile | Out-Null
                Assert-Exit "Create ECR repository $name"
            }
            if ($tfAddresses -notcontains $address) {
                Write-Host "Importing ECR repository into Terraform state: $name"
                & terraform import "-var=aws_account_id=$account" $address $name
                Assert-Exit "Import ECR repository $name"
            }
        }
        $sha = (& git -C $root rev-parse --short=12 HEAD).Trim()
        Assert-Exit 'Git revision'
        $tag = "phase5-$sha"
        $dockerFiles = @{
            lambda = 'Dockerfile.phase5-lambda'
            projector = 'Dockerfile.phase5-projector'
            generator = 'Dockerfile.phase5-generator'
            lifecycle = 'Dockerfile.phase5-lifecycle'
        }
        # Reuse the proven Windows ECR workaround from publish_ecr_image.ps1.
        # Docker login fails with HTTP 400 on this machine; push with an isolated
        # temporary config.json instead of the Docker Desktop credential helper.
        $tempConfig = $null
        $imageDigests = @{}
        try {
            $tempConfig = Join-Path ([IO.Path]::GetTempPath()) ('ecr-push-' + [guid]::NewGuid())
            New-Item -ItemType Directory -Path $tempConfig -ErrorAction Stop | Out-Null
            $token = & aws ecr get-login-password --profile $AwsProfile --region $Region
            Assert-Exit 'ECR authorization'
            if (-not $token) { throw 'Empty ECR token' }
            $auth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('AWS:' + $token))
            $dockerCfg = @{ auths = @{ $registry = @{ auth = $auth } } }
            [IO.File]::WriteAllText(
                (Join-Path $tempConfig 'config.json'),
                ($dockerCfg | ConvertTo-Json -Depth 5 -Compress),
                [Text.UTF8Encoding]::new($false)
            )
            Remove-Variable token, auth, dockerCfg -ErrorAction SilentlyContinue

            foreach ($kind in $repositories.Keys) {
            $name = $repositories[$kind]
            $digest = (& aws ecr list-images --repository-name $name --filter tagStatus=TAGGED --profile $AwsProfile --region $Region --query "imageIds[?imageTag=='$tag'].imageDigest | [0]" --output text).Trim()
            Assert-Exit "Inspect image tag $kind"
            if (-not $digest -or $digest -eq 'None') {
                $uri = "$registry/$name"
                $imageTag = [string]::Format('{0}:{1}', $uri, $tag)
                Write-Host "Building $kind from $($dockerFiles[$kind])"
                # Docker Desktop's default BuildKit output is an OCI index (often
                # with provenance attestations). AWS Lambda requires a single
                # platform image manifest rather than an OCI image index.
                # --load imports the single linux/amd64 image into Docker so our
                # established --config ECR authentication workaround still works.
                & docker buildx build --platform linux/amd64 --provenance=false --sbom=false --load --file (Join-Path $root $dockerFiles[$kind]) --tag $imageTag $root
                Assert-Exit "Docker buildx single-image build $kind"
                & docker --config $tempConfig push $imageTag
                Assert-Exit "Docker ECR push $kind"
                $digest = (& aws ecr describe-images --repository-name $name --image-ids "imageTag=$tag" --profile $AwsProfile --region $Region --query 'imageDetails[0].imageDigest' --output text).Trim()
                Assert-Exit "Resolve $kind ECR digest"
            }
            if ($digest -notmatch '^sha256:[a-f0-9]{64}$') {
                throw "Invalid ECR digest for $($kind): $digest"
            }
            $manifestType = (& aws ecr describe-images --repository-name $name --image-ids "imageTag=$tag" --profile $AwsProfile --region $Region --query 'imageDetails[0].imageManifestMediaType' --output text).Trim()
            Assert-Exit "Inspect ECR manifest media type $kind"
            if ($manifestType -notin @('application/vnd.docker.distribution.manifest.v2+json', 'application/vnd.oci.image.manifest.v1+json')) {
                throw "Unsupported ECR image media type for $($kind): $manifestType. Refusing Terraform apply."
            }
            Write-Host "ECR $kind verified: $manifestType $digest"
            $imageDigests[$kind] = "$registry/$name@$digest"
            }
        }
        finally {
            Remove-Variable token, auth, dockerCfg -ErrorAction SilentlyContinue
            if ($tempConfig) {
                Remove-Item -LiteralPath $tempConfig -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        $secretArn = (& aws secretsmanager describe-secret --secret-id $SecretId --profile $AwsProfile --region $Region --query ARN --output text).Trim()
        Assert-Exit 'Shopify app secret lookup'
        $vars = @(
            "-var=aws_account_id=$account",
            "-var=aws_profile=$AwsProfile",
            "-var=aws_region=$Region",
            "-var=shopify_shop_domain=$ShopDomain",
            "-var=shopify_client_credentials_secret_arn=$secretArn",
            "-var=shopify_webhook_secret_arn=$secretArn",
            "-var=phase5_lambda_image=$($imageDigests['lambda'])",
            "-var=phase5_projector_image=$($imageDigests['projector'])",
            "-var=phase5_generator_image=$($imageDigests['generator'])",
            "-var=phase5_lifecycle_image=$($imageDigests['lifecycle'])"
        )
        $planFile = Join-Path $root '.phase5-release.tfplan'
        Write-Host '=== Review Terraform plan: Phase 5 enabled ==='
        & terraform plan @vars "-out=$planFile"
        Assert-Exit 'Terraform plan'
        & terraform show $planFile
        Assert-Exit 'Terraform plan display'
        if (-not $ApprovePlan) {
            $confirmation = Read-Host 'Review resources and costs. Type APPLY-PHASE5 to apply this saved plan'
            if ($confirmation -cne 'APPLY-PHASE5') { throw 'Deployment stopped before Terraform apply' }
        }
        & terraform apply $planFile
        Assert-Exit 'Terraform apply'
        $schemaTask = (& terraform output -raw phase5_schema_task_definition).Trim()
        Assert-Exit 'Schema task ARN'
        $cluster = (& terraform output -raw ecs_cluster_name).Trim()
        Assert-Exit 'ECS cluster ARN'
        $subnets = @(& terraform output -json application_subnet_ids | ConvertFrom-Json)
        Assert-Exit 'Application subnets'
        $webhookUrl = (& terraform output -raw phase5_webhook_url).Trim()
        Assert-Exit 'Webhook HTTPS endpoint'
        $generatorDef = (& terraform output -raw phase5_generator_task_definition).Trim()
        Assert-Exit 'Generator task definition'
        $landingBucket = (& terraform output -raw landing_bucket_name).Trim()
        Assert-Exit 'Landing bucket'
        # The schema task uses the existing ECS API security group, with RDS access.
        $ecsSg = (& terraform output -raw phase5_schema_security_group_id).Trim()
        Assert-Exit 'Schema security group'
    }
    finally { Pop-Location }

    Write-Host '=== Phase 5: migrate private RDS before opening webhook intake ==='
    $dbStatus = (& aws rds describe-db-instances --db-instance-identifier ecommerce-postgres-dev --query 'DBInstances[0].DBInstanceStatus' --output text --profile $AwsProfile --region $Region).Trim()
    Assert-Exit 'RDS state'
    if ($dbStatus -eq 'stopped') {
        & aws rds start-db-instance --db-instance-identifier ecommerce-postgres-dev --profile $AwsProfile --region $Region | Out-Null
        Assert-Exit 'RDS wake for migration'
    }
    & aws rds wait db-instance-available --db-instance-identifier ecommerce-postgres-dev --profile $AwsProfile --region $Region
    Assert-Exit 'RDS available before migration'
    $net = @{
        awsvpcConfiguration = @{
            subnets = @($subnets)
            securityGroups = @($ecsSg)
            assignPublicIp = 'DISABLED'
        }
    } | ConvertTo-Json -Depth 7 -Compress
    $taskArn = (& aws ecs run-task --cluster $cluster --task-definition $schemaTask --launch-type FARGATE --count 1 --network-configuration $net --profile $AwsProfile --region $Region --query 'tasks[0].taskArn' --output text).Trim()
    Assert-Exit 'Start schema migration'
    if (-not $taskArn -or $taskArn -eq 'None') { throw 'ECS schema task was not launched' }
    & aws ecs wait tasks-stopped --cluster $cluster --tasks $taskArn --profile $AwsProfile --region $Region
    Assert-Exit 'Wait for schema migration'
    $exitCode = (& aws ecs describe-tasks --cluster $cluster --tasks $taskArn --profile $AwsProfile --region $Region --query 'tasks[0].containers[0].exitCode' --output text).Trim()
    Assert-Exit 'Migration exit inspection'
    if ($exitCode -ne '0') {
        & aws logs tail "/ecs/ecommerce-$Environment/phase5-schema" --since 20m --profile $AwsProfile --region $Region
        throw "Schema migration failed with exit code $exitCode"
    }

    & python (Join-Path $root 'scripts/phase5_mark_schema_ready.py') --profile $AwsProfile --region $Region --environment $Environment
    Assert-Exit 'Record operational schema readiness'
    Write-Host '=== Phase 5: register project-app Shopify HTTPS webhooks ==='
    & python (Join-Path $root 'scripts/register_phase5_shopify_webhooks.py') --profile $AwsProfile --region $Region --secret-id $SecretId --shop $ShopDomain --url $webhookUrl --apply
    Assert-Exit 'Shopify HTTPS subscription registration'
    & python (Join-Path $root 'scripts/phase5_mark_schema_ready.py') --profile $AwsProfile --region $Region --environment $Environment --webhooks-ready
    Assert-Exit 'Record confirmed Shopify webhook readiness'
    Write-Host '=== Phase 5: one-order Shopify smoke test ==='
    $smokeRunId = "smoke-$sha-$(Get-Date -Format yyyyMMdd)"
    $override = @{ containerOverrides = @(@{
        name='phase5-generator'
        command=@('--slot','morning','--apply','--smoke-run-id',$smokeRunId)
    }) } | ConvertTo-Json -Depth 6 -Compress
    $smokeTask = (& aws ecs run-task --cluster $cluster --task-definition $generatorDef --launch-type FARGATE --count 1 --network-configuration $net --overrides $override --profile $AwsProfile --region $Region --query 'tasks[0].taskArn' --output text).Trim()
    Assert-Exit 'Start Shopify smoke order'
    if (-not $smokeTask -or $smokeTask -eq 'None') { throw 'Shopify smoke task did not launch' }
    & aws ecs wait tasks-stopped --cluster $cluster --tasks $smokeTask --profile $AwsProfile --region $Region
    Assert-Exit 'Wait for Shopify smoke order'
    $smokeCode = (& aws ecs describe-tasks --cluster $cluster --tasks $smokeTask --profile $AwsProfile --region $Region --query 'tasks[0].containers[0].exitCode' --output text).Trim()
    Assert-Exit 'Inspect Shopify smoke result'
    if ($smokeCode -ne '0') {
        & aws logs tail "/ecs/ecommerce-$Environment/phase5-generator" --since 20m --profile $AwsProfile --region $Region
        throw "Shopify smoke order failed with exit code $smokeCode"
    }
    & python (Join-Path $root 'scripts/verify_phase5_smoke.py') --profile $AwsProfile --region $Region --environment $Environment --bucket $landingBucket --run-id $smokeRunId
    Assert-Exit 'Shopify SQS S3 RDS smoke verification'
    Write-Host "PHASE 5 DEPLOYED AND SMOKE VERIFIED. Webhook: $webhookUrl"
    Write-Host 'Schedules: 08:00 (20), 16:00 (25), 18:00 lifecycle — Europe/Berlin.'
    Write-Host 'SQS landing and minute recovery enabled; RDS idle stop is 30 minutes.'
    Write-Host 'Check CloudWatch logs and verify an actual Shopify order before declaring end-to-end success.'
}
finally { Pop-Location }
