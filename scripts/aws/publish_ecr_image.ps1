<#
Build and optionally publish an immutable ECR image. Preview-only by default.
Examples:
  .\scripts\aws\publish_ecr_image.ps1
  .\scripts\aws\publish_ecr_image.ps1 -Execute -ConfirmPublish
The temporary Docker config workaround matches the engineering runbook.
#>
[CmdletBinding()]
param(
    [string]$ConfigPath = 'config/aws-demo.json',
    [ValidateSet('customer_sync','catalog_sync','phase5_migration','phase5_pricing')][string]$Workload = 'customer_sync',
    [string]$ImageTag,
    [switch]$Execute,
    [switch]$ConfirmPublish
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed (exit code $LASTEXITCODE)" }
}

$root = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$cfgPath = Join-Path $root $ConfigPath
$config = Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json
$work = $config.$Workload
$common = @('--region', "$($config.region)", '--profile', "$($config.profile)")
$account = & aws sts get-caller-identity @common --query Account --output text
Assert-Exit 'AWS identity'
if ("$account".Trim() -ne "$($config.account_id)") { throw 'Unexpected AWS account' }
$sha = & git -C $root rev-parse HEAD
Assert-Exit 'Git revision'
$sha = "$sha".Trim()
if (-not $ImageTag) { $ImageTag = 'git-' + $sha.Substring(0, 12) }
if ($ImageTag -notmatch '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$') {
    throw 'Invalid ECR image tag'
}
& git -C $root diff --quiet
$uncommitted = $LASTEXITCODE
& git -C $root diff --cached --quiet
if ($uncommitted -ne 0 -or $LASTEXITCODE -ne 0) {
    throw 'Commit or stash tracked changes before building a versioned image'
}
$registry = "$($config.account_id).dkr.ecr.$($config.region).amazonaws.com"
$repository = "$($work.repository)"
$remote = $registry + '/' + $repository + ':' + $ImageTag
$local = $repository + ':' + $ImageTag
$dockerfile = Join-Path $root "$($work.dockerfile)"
if (-not (Test-Path $dockerfile)) { throw 'Dockerfile missing' }
Write-Host "Git revision: $sha"
Write-Host "Dockerfile: $dockerfile"
Write-Host "Target: $remote"
if (-not $Execute) { Write-Host 'PREVIEW ONLY: add -Execute -ConfirmPublish'; return }
if (-not $ConfirmPublish) { throw 'Explicit -ConfirmPublish required' }

# Listing tagged images succeeds with an empty collection for a new repository.
# Unlike describe-images with --image-ids, a missing tag is not an AWS CLI error.
# Any genuine AWS/API failure must stop publication rather than be swallowed.
$existing = & aws ecr list-images --repository-name $repository --filter tagStatus=TAGGED @common --query "imageIds[?imageTag=='$ImageTag'].imageDigest" --output text
Assert-Exit 'ECR image tag availability check'
if ("$existing".Trim()) {
    throw 'Immutable image tag already exists. Use a new committed revision/tag.'
}
Push-Location $root
$tempConfig = $null
try {
    if ($Workload -eq 'customer_sync') {
        & python -m pytest -q tests/unit/test_shopify_customer_sync.py tests/unit/test_shopify_sync_ledger.py tests/unit/test_customer_sync_runner.py
    } elseif ($Workload -eq 'catalog_sync') {
        & python -m pytest -q tests/unit/test_catalog_selection.py tests/unit/test_catalog_sync.py tests/unit/test_catalog_persistence.py tests/unit/test_shopify_catalog_client.py
    } elseif ($Workload -eq 'phase5_migration') {
        & python -m pytest -q tests/unit/test_phase5_price_migration.py
    } else {
        & python -m pytest -q tests/unit/test_variant_pricing.py tests/unit/test_shopify_gbp_pricing.py tests/unit/test_phase5_price_audit.py
    }
    Assert-Exit 'Workload regression tests'
    & docker build --file $dockerfile --label "org.opencontainers.image.revision=$sha" --tag $local .
    Assert-Exit 'Docker build'
    & docker tag $local $remote
    Assert-Exit 'Docker tag'
    $tempConfig = Join-Path ([IO.Path]::GetTempPath()) ('ecr-push-' + [guid]::NewGuid())
    New-Item -ItemType Directory -Path $tempConfig -ErrorAction Stop | Out-Null
    $token = & aws ecr get-login-password @common
    Assert-Exit 'ECR authorization'
    if (-not $token) { throw 'Empty ECR token' }
    $auth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes('AWS:' + $token))
    $dockerCfg = @{ auths = @{ $registry = @{ auth = $auth } } }
    [IO.File]::WriteAllText((Join-Path $tempConfig 'config.json'), ($dockerCfg | ConvertTo-Json -Depth 5 -Compress), [Text.UTF8Encoding]::new($false))
    Remove-Variable token, auth, dockerCfg -ErrorAction SilentlyContinue
    & docker --config $tempConfig push $remote
    Assert-Exit 'Docker ECR push'
    $digest = & aws ecr describe-images --repository-name $repository --image-ids "imageTag=$ImageTag" @common --query 'imageDetails[0].imageDigest' --output text
    Assert-Exit 'ECR digest lookup'
    if ("$digest" -notmatch '^sha256:[a-f0-9]{64}$') { throw 'Invalid registry digest' }
    Write-Host ('Verified immutable ECR image: ' + $registry + '/' + $repository + '@' + $digest)
    Write-Host 'No ECS task launched; Terraform and Shopify unchanged.'
}
finally {
    Remove-Variable token, auth, dockerCfg -ErrorAction SilentlyContinue
    if ($tempConfig) {
        Remove-Item -LiteralPath $tempConfig -Recurse -Force -ErrorAction SilentlyContinue
    }
    Pop-Location
}
