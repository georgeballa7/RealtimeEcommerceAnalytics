<#
Publish the Phase 6 dbt container to the new Terraform-created ECR repository.
Uses the proven Phase 5 Docker Desktop temporary config workaround.
No Terraform apply, ECS launch, Glue run or Shopify changes.
Run ONLY after deploy_phase6_dbt.ps1 -Apply created the ECR repo.
#>
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Profile = 'ecommerce-admin'
$Region = 'eu-central-1'
$AccountId = '825414820081'
$Repository = 'ecommerce-phase6-dbt-demo'
$Registry = "$AccountId.dkr.ecr.$Region.amazonaws.com"
$DockerConfig = $null

function Assert-Exit([string]$Label) {
    if ($LASTEXITCODE -ne 0) { throw "$Label failed ($LASTEXITCODE)" }
}

Push-Location $Root
try {
    $Account = (& aws sts get-caller-identity --profile $Profile --region $Region --query Account --output text | Out-String).Trim()
    Assert-Exit 'AWS identity'
    if ($Account -ne $AccountId) { throw 'Incorrect AWS account' }
    $Branch = (& git branch --show-current | Out-String).Trim()
    Assert-Exit 'Git branch'
    if ($Branch -ne 'feature/dbt-analytics-churn-model') { throw "Unexpected branch: $Branch" }
    $changes = @(& git status --porcelain)
    Assert-Exit 'Git working tree'
    if ($changes.Count -gt 0) { throw 'Commit or stash changes before publishing the immutable dbt image' }
    $Commit = (& git rev-parse --short=12 HEAD | Out-String).Trim()
    Assert-Exit 'Git commit'
    $Tag = "dbt-$Commit"
    $ImageTag = "$Registry/${Repository}:$Tag"

    & aws ecr describe-repositories --repository-names $Repository --profile $Profile --region $Region --output json | Out-Null
    Assert-Exit 'dbt ECR repository readiness'
    $Digest = (& aws ecr list-images --repository-name $Repository --filter tagStatus=TAGGED --query "imageIds[?imageTag=='$Tag'].imageDigest | [0]" --profile $Profile --region $Region --output text | Out-String).Trim()
    Assert-Exit 'Inspect existing ECR image'
    if (-not $Digest -or $Digest -eq 'None') {
        & docker buildx build --platform linux/amd64 --provenance=false --sbom=false --load --file (Join-Path $Root 'docker/Dockerfile.phase6-dbt') --tag $ImageTag $Root | Out-Host
        Assert-Exit 'Build dbt image'
        $DockerConfig = Join-Path ([IO.Path]::GetTempPath()) ("dbt-ecr-" + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $DockerConfig | Out-Null
        $token = & aws ecr get-login-password --profile $Profile --region $Region
        Assert-Exit 'ECR login token'
        $auth = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("AWS:$token"))
        $config = @{auths = @{$Registry = @{auth = $auth}}} | ConvertTo-Json -Compress -Depth 6
        [IO.File]::WriteAllText((Join-Path $DockerConfig 'config.json'), $config, (New-Object Text.UTF8Encoding($false)))
        Remove-Variable token,auth -ErrorAction SilentlyContinue
        & docker --config $DockerConfig push $ImageTag | Out-Host
        Assert-Exit 'Push dbt ECR image'
        $Digest = (& aws ecr describe-images --repository-name $Repository --image-ids "imageTag=$Tag" --query "imageDetails[0].imageDigest" --profile $Profile --region $Region --output text | Out-String).Trim()
        Assert-Exit 'Verify dbt image digest'
    }
    if ($Digest -notmatch '^sha256:[a-f0-9]{64}$') { throw 'Invalid ECR image digest' }
    $Media = (& aws ecr describe-images --repository-name $Repository --image-ids "imageTag=$Tag" --query "imageDetails[0].imageManifestMediaType" --profile $Profile --region $Region --output text | Out-String).Trim()
    Assert-Exit 'Inspect dbt ECR manifest'
    if ($Media -notin @('application/vnd.oci.image.manifest.v1+json','application/vnd.docker.distribution.manifest.v2+json')) {
        throw "Unexpected ECR image manifest type: $Media"
    }
    $Pinned = "$Registry/$Repository@$Digest"
    Write-Host 'Verified immutable Phase 6 dbt image:'
    Write-Host $Pinned
    Write-Host 'Next: deploy_phase6_dbt.ps1 -PinnedImage <above-URI> (preview), then -Apply.'
}
finally {
    Pop-Location
    if ($DockerConfig) { Remove-Item -LiteralPath $DockerConfig -Force -Recurse -ErrorAction SilentlyContinue }
}
