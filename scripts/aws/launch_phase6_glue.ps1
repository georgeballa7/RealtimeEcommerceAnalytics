<#
Prepare the on-demand Phase 6 Glue release without destroying existing Phase 5.
Usage from repo root:
  .\scripts\aws\launch_phase6_glue.ps1              # plan only
  .\scripts\aws\launch_phase6_glue.ps1 -Apply       # apply reviewed, allowlisted plan
  .\scripts\aws\launch_phase6_glue.ps1 -Launch      # plan/apply, start RDS, run Glue, verify manifest

All CLI calls use the ecommerce-admin AWS profile. No recurring schedule created.
#>
[CmdletBinding()]
param(
    [switch]$Apply,
    [switch]$Launch,
    [switch]$UseGlueLease
)
$ErrorActionPreference = "Stop"
if ($UseGlueLease -and -not $Launch) {
    throw "-UseGlueLease requires -Launch"
}
Set-StrictMode -Version Latest
$ProfileName = "ecommerce-admin"
$Region = "eu-central-1"
$ExpectedAccount = "825414820081"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$TerraformDir = Join-Path $Root "infrastructure\terraform"
$JobName = "ecommerce-phase6-shopify-silver-demo"
$DbId = "ecommerce-postgres-dev"
$Bucket = "ecommerce-demo-landing-393e7fdc5c01861ae4e5c25ad6"

function Invoke-AwsText([string[]]$AwsArgs) {
    $out = & aws @AwsArgs --region $Region --profile $ProfileName --output text
    if ($LASTEXITCODE -ne 0) { throw "AWS CLI failed: $($AwsArgs -join ' ')" }
    $value = ($out | Out-String).Trim()
    if (-not $value -or $value -eq "None") {
        throw "Missing AWS value: $($AwsArgs -join ' ')"
    }
    return $value
}

$account = Invoke-AwsText -AwsArgs @("sts", "get-caller-identity", "--query", "Account")
if ($account -ne $ExpectedAccount) { throw "Unexpected AWS account: $account" }
$domain = Invoke-AwsText -AwsArgs @(
    "lambda", "get-function-configuration", "--function-name", "ecommerce-demo-phase5-fastapi",
    "--query", "Environment.Variables.SHOPIFY_SHOP_DOMAIN"
)
$secretArn = Invoke-AwsText -AwsArgs @(
    "lambda", "get-function-configuration", "--function-name", "ecommerce-demo-phase5-fastapi",
    "--query", "Environment.Variables.SHOPIFY_WEBHOOK_SECRET_ARN"
)
$lambdaImage = Invoke-AwsText -AwsArgs @(
    "lambda", "get-function", "--function-name", "ecommerce-demo-phase5-fastapi",
    "--query", "Code.ImageUri"
)
$generatorTask = Invoke-AwsText -AwsArgs @(
    "scheduler", "get-schedule", "--name", "ecommerce-demo-phase5-morning",
    "--query", "Target.EcsParameters.TaskDefinitionArn"
)
$lifecycleTask = Invoke-AwsText -AwsArgs @(
    "scheduler", "get-schedule", "--name", "ecommerce-demo-phase5-lifecycle",
    "--query", "Target.EcsParameters.TaskDefinitionArn"
)
$projectorTask = Invoke-AwsText -AwsArgs @(
    "lambda", "get-function-configuration", "--function-name", "ecommerce-demo-phase5-controller",
    "--query", "Environment.Variables.PROJECTOR_TASK_DEFINITION"
)
$generatorImage = Invoke-AwsText -AwsArgs @("ecs", "describe-task-definition", "--task-definition", $generatorTask, "--query", "taskDefinition.containerDefinitions[0].image")
$lifecycleImage = Invoke-AwsText -AwsArgs @("ecs", "describe-task-definition", "--task-definition", $lifecycleTask, "--query", "taskDefinition.containerDefinitions[0].image")
$projectorImage = Invoke-AwsText -AwsArgs @("ecs", "describe-task-definition", "--task-definition", $projectorTask, "--query", "taskDefinition.containerDefinitions[0].image")

# The live Lambda environment is the rollout gate: never assume a controller
# can honor Glue leases merely because code exists in the Git branch.
if ($UseGlueLease) {
    $liveGlueJob = Invoke-AwsText -AwsArgs @(
        "lambda", "get-function-configuration", "--function-name",
        "ecommerce-demo-phase5-controller", "--query",
        "Environment.Variables.PHASE6_GLUE_JOB_NAME"
    )
    if ($liveGlueJob -ne $JobName) {
        throw "Live RDS controller is not configured for this Glue lease. Do not launch protected mode."
    }
}

# The proven Phase 5 release supplied these exact settings via Terraform CLI.
# Avoid native PowerShell argument splitting entirely: serialize variables to a
# temporary .tfvars.json file and pass ONE -var-file=... argument to Terraform.
# A CLI var-file has higher precedence than any local terraform.tfvars file.
$tfSettings = [ordered]@{
    aws_account_id = $ExpectedAccount
    aws_profile = $ProfileName
    aws_region = $Region
    shopify_shop_domain = $domain
    shopify_client_credentials_secret_arn = $secretArn
    shopify_webhook_secret_arn = $secretArn
    phase5_lambda_image = $lambdaImage
    phase5_generator_image = $generatorImage
    phase5_projector_image = $projectorImage
    phase5_lifecycle_image = $lifecycleImage
    enable_redshift_warehouse = $true
    enable_phase6_glue_silver = $true
    enable_phase6_glue_rds_lease = [bool]$UseGlueLease
}
$tfvarsPath = Join-Path ([System.IO.Path]::GetTempPath()) (
    "phase6-glue-" + [guid]::NewGuid().ToString("N") + ".tfvars.json"
)
[System.IO.File]::WriteAllText(
    $tfvarsPath,
    ($tfSettings | ConvertTo-Json -Depth 5 -Compress),
    (New-Object System.Text.UTF8Encoding($false))
)

Push-Location $TerraformDir
try {
    & terraform init -input=false
    if ($LASTEXITCODE -ne 0) { throw "terraform init failed" }
    & terraform fmt -check phase6_redshift.tf phase6_rds_exporter.tf phase6_glue_silver.tf
    if ($LASTEXITCODE -ne 0) { throw "terraform fmt check failed" }
    & terraform validate
    if ($LASTEXITCODE -ne 0) { throw "terraform validate failed" }
    & terraform plan "-var-file=$tfvarsPath" "-out=phase6-glue-reviewed.tfplan"
    if ($LASTEXITCODE -ne 0) { throw "terraform plan failed" }

    $planJson = (& terraform show -json .\phase6-glue-reviewed.tfplan | Out-String)
    if ($LASTEXITCODE -ne 0) { throw "Could not inspect Terraform plan" }
    $plan = $planJson | ConvertFrom-Json
    $changes = @($plan.resource_changes | Where-Object {
        ($_.change.actions -join ",") -ne "no-op"
    })
    $allowed = @(
        'aws_glue_job.phase6_shopify_silver[0]',
        'aws_glue_connection.phase6_rds_network[0]',
        'aws_iam_role.phase6_glue[0]',
        'aws_iam_role_policy_attachment.phase6_glue_service[0]',
        'aws_iam_role_policy.phase6_glue_data[0]',
        'aws_security_group.phase6_glue[0]',
        'aws_vpc_security_group_ingress_rule.phase6_glue_self[0]',
        'aws_vpc_security_group_egress_rule.phase6_glue_outbound[0]',
        'aws_vpc_security_group_ingress_rule.rds_from_phase6_glue[0]',
        'aws_s3_object.phase6_glue_silver_script[0]'
    )
    $changes | ForEach-Object {
        Write-Host ("{0}: {1}" -f $_.address, ($_.change.actions -join ","))
    }
    $unsafe = @($changes | Where-Object {
        $actions = ($_.change.actions -join ",")
        $allowScriptRefresh = (
            $_.address -eq 'aws_s3_object.phase6_glue_silver_script[0]' -and
            $actions -eq "update"
        )
        ($_.address -notin $allowed) -or
        ($_.change.actions -contains "delete") -or
        (($actions -ne "create") -and (-not $allowScriptRefresh))
    })
    if ($unsafe.Count -gt 0) {
        throw "UNSAFE Terraform diff: changes outside the Phase 6 Glue allowlist. No apply performed."
    }
    if ($changes.Count -eq 0) {
        Write-Host "Terraform is already converged: no resource changes required."
    }
    if (-not $Apply -and -not $Launch) {
        Write-Host "Plan validated. Re-run with -Apply, or -Launch to deploy and run the exporter."
        return
    }
    & terraform apply -input=false .\phase6-glue-reviewed.tfplan
    if ($LASTEXITCODE -ne 0) { throw "Terraform apply failed" }
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $tfvarsPath -Force -ErrorAction SilentlyContinue
}
if (-not $Launch) {
    Write-Host "Glue job deployed. No job has run and no schedule was added."
    return
}

$leaseOwner = $null
$leaseRunAttached = $false
$LeaseHelper = Join-Path $Root "scripts\aws\phase6_glue_rds_lease.py"
try {
    if ($UseGlueLease) {
        $leaseReservation = & python $LeaseHelper reserve --profile $ProfileName --region $Region
        if ($LASTEXITCODE -ne 0) { throw "Unable to acquire Glue RDS processing lease" }
        $leaseOwner = (($leaseReservation | Out-String) | ConvertFrom-Json).owner
        if (-not $leaseOwner) { throw "Lease helper did not return a reservation owner" }
        Write-Host "Acquired controller-coordinated Glue RDS lease."
    }

# RDS can be in a scheduled idle-stop transition as the launch begins.
# Finish that transition before calling StartDBInstance; never issue a start
# against a "stopping" instance. AWS CLI waiters use read-only describe calls.
$rdsStatus = Invoke-AwsText -AwsArgs @(
    "rds", "describe-db-instances", "--db-instance-identifier", $DbId,
    "--query", "DBInstances[0].DBInstanceStatus"
)
if ($rdsStatus -eq "stopping") {
    Write-Host "RDS is stopping. Waiting for STOPPED before restarting for Glue."
    & aws rds wait db-instance-stopped --db-instance-identifier $DbId --region $Region --profile $ProfileName
    if ($LASTEXITCODE -ne 0) { throw "RDS did not complete its stop transition" }
    $rdsStatus = Invoke-AwsText -AwsArgs @(
        "rds", "describe-db-instances", "--db-instance-identifier", $DbId,
        "--query", "DBInstances[0].DBInstanceStatus"
    )
}
if ($rdsStatus -eq "stopped") {
    Write-Host "Starting RDS for the on-demand Glue snapshot."
    & aws rds start-db-instance --db-instance-identifier $DbId --region $Region --profile $ProfileName | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Failed to start RDS" }
}
elseif ($rdsStatus -ne "available" -and $rdsStatus -ne "starting" -and $rdsStatus -ne "configuring-enhanced-monitoring") {
    throw "RDS status $rdsStatus does not allow safe Glue execution"
}
if ($rdsStatus -ne "available") {
    & aws rds wait db-instance-available --db-instance-identifier $DbId --region $Region --profile $ProfileName
    if ($LASTEXITCODE -ne 0) { throw "RDS did not reach available status" }
}
# Defend against the controller reinitiating idle-stop while we were waiting.
$rdsFinalStatus = Invoke-AwsText -AwsArgs @(
    "rds", "describe-db-instances", "--db-instance-identifier", $DbId,
    "--query", "DBInstances[0].DBInstanceStatus"
)
if ($rdsFinalStatus -ne "available") {
    throw "RDS changed state to $rdsFinalStatus; refusing Glue snapshot"
}

$batchId = (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ")
$runArgsFile = Join-Path ([System.IO.Path]::GetTempPath()) ("phase6-glue-" + $batchId + ".json")
[System.IO.File]::WriteAllText(
    $runArgsFile, (@{ "--batch-id" = $batchId } | ConvertTo-Json -Compress),
    (New-Object System.Text.UTF8Encoding($false))
)
try {
    $runId = Invoke-AwsText -AwsArgs @(
        "glue", "start-job-run", "--job-name", $JobName,
        "--arguments", ("file://" + $runArgsFile),
        "--query", "JobRunId"
    )
}
finally {
    Remove-Item -LiteralPath $runArgsFile -ErrorAction SilentlyContinue
}
Write-Host "Glue started: $JobName / $runId (batch $batchId)"
if ($UseGlueLease) {
    & python $LeaseHelper attach --owner $leaseOwner --run-id $runId --profile $ProfileName --region $Region
    if ($LASTEXITCODE -ne 0) {
        throw "Glue started but lease attachment failed; leaving lease for reconciliation"
    }
    $leaseRunAttached = $true
}
for ($poll = 0; $poll -lt 45; $poll++) {
    Start-Sleep -Seconds 30
    if ($UseGlueLease) {
        & python $LeaseHelper renew --owner $leaseOwner --profile $ProfileName --region $Region
        if ($LASTEXITCODE -ne 0) {
            throw "Glue lease renewal failed; keep RDS available and reconcile job manually"
        }
    }
    $run = & aws glue get-job-run --job-name $JobName --run-id $runId --region $Region --profile $ProfileName --output json | ConvertFrom-Json
    if ($LASTEXITCODE -ne 0) { throw "Glue run status unavailable" }
    $state = $run.JobRun.JobRunState
    Write-Host "Glue run status: $state"
    if ($state -eq "SUCCEEDED") { break }
    if ($state -in @("FAILED", "STOPPED", "TIMEOUT", "ERROR", "EXPIRED")) {
        throw "Glue failed: $($run.JobRun.ErrorMessage)"
    }
}
if ($state -ne "SUCCEEDED") {
    throw "Glue did not finish within the launch polling window"
}
$manifestKey = "warehouse/shopify_snapshots/v1/$batchId/silver_manifest.json"
$manifestBody = & aws s3 cp "s3://$Bucket/$manifestKey" - --region $Region --profile $ProfileName
if ($LASTEXITCODE -ne 0) { throw "Glue run succeeded but Silver manifest is missing" }
$manifest = ($manifestBody | Out-String) | ConvertFrom-Json
if ($manifest.schema_version -ne "shopify-live-silver-v1") {
    throw "Unexpected Silver manifest schema_version"
}
Write-Host "Verified Silver manifest: s3://$Bucket/$manifestKey"
$manifest.counts | ConvertTo-Json -Depth 8

}
finally {
    if ($UseGlueLease -and $leaseOwner) {
        if ($leaseRunAttached) {
            # The helper releases only after authoritative Glue TERMINAL state.
            & python $LeaseHelper release --owner $leaseOwner --profile $ProfileName --region $Region
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Glue lease retained: run has not been verified terminal. Reconcile manually."
            }
        }
        else {
            Write-Warning "Glue lease retained because run attachment was not verified. Reconcile before retry."
        }
    }
}
