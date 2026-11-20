# Unit-level PowerShell integration test for the Phase 6 launch wrapper.
# No AWS credentials, Terraform backend access, or infrastructure mutations.
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$entry = Join-Path $root 'scripts/aws/launch_phase6_glue.ps1'
$global:ObservedPlan = $false
$global:MockUnsafe = $false
$global:MockScriptRefresh = $false
$global:MockApplyCalls = 0

function global:aws {
    $global:LASTEXITCODE = 0
    $argv = @($args)
    $qPos = [array]::IndexOf($argv, '--query')
    if ($qPos -lt 0) { throw ('Unexpected mock AWS command: ' + ($argv -join ' ')) }
    $query = $argv[$qPos + 1]
    switch ($query) {
        'Account' { return '825414820081' }
        'Environment.Variables.SHOPIFY_SHOP_DOMAIN' {
            return 'realtime-ecommerce-analytics.myshopify.com'
        }
        'Environment.Variables.SHOPIFY_WEBHOOK_SECRET_ARN' {
            return 'arn:aws:secretsmanager:eu-central-1:825414820081:secret:shopify/test'
        }
        'Environment.Variables.PROJECTOR_TASK_DEFINITION' {
            return 'arn:aws:ecs:eu-central-1:825414820081:task-definition/projector:3'
        }
        'Code.ImageUri' { return '123456789012.dkr.ecr.eu-central-1.amazonaws.com/lambda@sha256:abc' }
        'Target.EcsParameters.TaskDefinitionArn' {
            if ($argv -contains 'ecommerce-demo-phase5-morning') { return 'generator:4' }
            if ($argv -contains 'ecommerce-demo-phase5-lifecycle') { return 'lifecycle:2' }
            throw 'Unexpected scheduler'
        }
        'taskDefinition.containerDefinitions[0].image' {
            $i = [array]::IndexOf($argv, '--task-definition')
            if ($i -lt 0) { throw 'Missing ECS task definition' }
            return ('mock-image:' + $argv[$i + 1])
        }
        default { throw ('Unexpected AWS query: ' + $query) }
    }
}

function global:terraform {
    $global:LASTEXITCODE = 0
    $argv = @($args)
    switch ($argv[0]) {
        'init' { return 'mock initialized' }
        'fmt' { return 'mock format passed' }
        'validate' { return 'mock configuration valid' }
        'plan' {
            if ($argv.Count -ne 3) {
                throw ('Expected exactly 3 Terraform plan arguments, found ' +
                       $argv.Count + ': ' + ($argv -join ' | '))
            }
            $varArg = @($argv | Where-Object { $_ -like '-var-file=*' })
            $outArg = @($argv | Where-Object { $_ -like '-out=*' })
            if ($varArg.Count -ne 1 -or $outArg.Count -ne 1) {
                throw ('Unexpected Terraform plan CLI: ' + ($argv -join ' | '))
            }
            $file = $varArg[0].Substring(10)
            if (-not (Test-Path -LiteralPath $file)) { throw 'Missing temp tfvars JSON' }
            if (-not $file.EndsWith('.tfvars.json')) { throw 'Incorrect var-file extension' }
            $settings = Get-Content -LiteralPath $file -Raw | ConvertFrom-Json
            if ($settings.aws_account_id -ne '825414820081' -or
                $settings.enable_phase6_glue_silver -ne $true -or
                $settings.enable_redshift_warehouse -ne $true -or
                $settings.phase5_generator_image -notlike 'mock-image:*') {
                throw 'Terraform var-file lost critical existing Phase 5 configuration'
            }
            $global:ObservedPlan = $true
            return 'mock plan completed'
        }
        'show' {
            if ($argv.Count -ne 3 -or $argv[1] -ne '-json') {
                throw 'Unexpected show arguments'
            }
            if ($global:MockUnsafe) {
                return '{"resource_changes":[{"address":"aws_scheduler_schedule.phase5_generation","change":{"actions":["update"]}}]}'
            }
            if ($global:MockScriptRefresh) {
                return '{"resource_changes":[{"address":"aws_s3_object.phase6_glue_silver_script[0]","change":{"actions":["update"]}}]}'
            }
            return '{"resource_changes":[{"address":"aws_glue_job.phase6_shopify_silver[0]","change":{"actions":["create"]}}]}'
        }
        'apply' {
            $global:MockApplyCalls += 1
            return 'mock apply success'
        }
        default { throw ('Unexpected Terraform command ' + ($argv -join ' ')) }
    }
}

# A read-only plan must be accepted and the temporary var-file cleaned.
& $entry
if (-not $global:ObservedPlan) { throw 'Terraform plan was not invoked' }
if ($global:MockApplyCalls -ne 0) { throw 'Plan-only mode tried applying resources' }

# Fail closed on a Phase 5 schedule update, including when -Apply was requested.
$global:MockUnsafe = $true
$blocked = $false
try { & $entry -Apply } catch {
    if ($_.Exception.Message -match 'UNSAFE Terraform diff') { $blocked = $true } else { throw }
}
if (-not $blocked) { throw 'Unsafe Terraform change was not rejected' }
if ($global:MockApplyCalls -ne 0) { throw 'Unsafe Terraform change reached apply' }

Write-Host 'Verified: Terraform native args, JSON var-file, Phase 5 preservation, allowlist rejection.'

# A change to the Glue source-script object alone is a safe applied update.
$global:MockUnsafe = $false
$global:MockScriptRefresh = $true
& $entry -Apply
if ($global:MockApplyCalls -ne 1) {
    throw 'Glue script refresh was unexpectedly blocked or applied more than once'
}
Write-Host 'Verified: only the Glue script object can update in-place.'
