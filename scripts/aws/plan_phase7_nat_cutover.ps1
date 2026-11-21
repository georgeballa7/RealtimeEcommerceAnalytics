<#
.SYNOPSIS
    Phase 7 on-demand NAT Terraform PLAN ONLY. No AWS mutations or apply.
.DESCRIPTION
    Reads existing Terraform state and the live NAT/route/schedule baseline.
    Reconstructs the deployed image digests and optional Phase 6 flags, then
    creates and inspects a temporary Terraform plan. Rejects ANY change beyond
    deletion of one static NAT/two default routes plus creation of the eight
    reviewed on-demand NAT Lambda/IAM/Scheduler components.
    Does not have an -Apply parameter or save an executable plan artifact.
.EXAMPLE
    .\scripts\aws\plan_phase7_nat_cutover.ps1
#>
[CmdletBinding()]
param()
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$AwsProfile = 'ecommerce-admin'
$Region = 'eu-central-1'
$AccountId = '825414820081'
$ExpectedBranch = 'feature/dbt-analytics-churn-model'
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$TerraformDir = Join-Path $Root 'infrastructure\terraform'
$Guard = Join-Path $Root 'scripts\aws\phase7_nat_cutover_plan.py'
$Temp = Join-Path ([IO.Path]::GetTempPath()) ('phase7-nat-review-' + [guid]::NewGuid().ToString('N'))

function Check-Exit([string]$Step) {
    if ($LASTEXITCODE -ne 0) { throw "$Step failed with exit code $LASTEXITCODE" }
}
function Read-AwsJson([string[]]$Arguments) {
    $raw = & aws @Arguments --profile $AwsProfile --region $Region --output json
    Check-Exit 'AWS read'
    return ($raw | Out-String | ConvertFrom-Json)
}

Push-Location $Root
try {
    $branch = (& git branch --show-current | Out-String).Trim()
    Check-Exit 'git branch'
    if ($branch -ne $ExpectedBranch) { throw "Expected branch $ExpectedBranch, found $branch" }
    $dirty = @(& git status --porcelain)
    Check-Exit 'git status'
    if ($dirty.Count -gt 0) { throw 'Working tree must be clean before plan review' }

    $identity = Read-AwsJson -Arguments @('sts', 'get-caller-identity')
    if ([string]$identity.Account -ne $AccountId) { throw 'Unexpected AWS account' }

    New-Item -ItemType Directory -Path $Temp -Force | Out-Null
    $statePath = Join-Path $Temp 'state.json'
    $varsPath = Join-Path $Temp 'cutover.tfvars.json'
    $planPath = Join-Path $Temp 'cutover.tfplan'
    $planJson = Join-Path $Temp 'cutover-plan.json'
    Push-Location $TerraformDir
    try {
        & terraform init -input=false -no-color
        Check-Exit 'terraform init'
        & terraform validate -no-color
        Check-Exit 'terraform validate'

        # Terraform CLI show -json reads state; it does NOT modify AWS.
        # Write sensitive state to a temporary location and never print it.
        $stateText = & terraform show -json
        Check-Exit 'terraform show state'
        [IO.File]::WriteAllText(
            $statePath, ($stateText | Out-String),
            (New-Object System.Text.UTF8Encoding($false))
        )
        & python -X utf8 $Guard snapshot --state $statePath --output $varsPath --account $AccountId
        Check-Exit 'Validate Terraform state and preserve deployed variables'

        $state = Get-Content -LiteralPath $statePath -Raw | ConvertFrom-Json
        $resources = @($state.values.root_module.resources)
        $natRows = @($resources | Where-Object { $_.address -eq 'aws_nat_gateway.ecommerce[0]' })
        $eipRows = @($resources | Where-Object { $_.address -eq 'aws_eip.nat[0]' })
        if ($natRows.Count -ne 1 -or $eipRows.Count -ne 1) {
            throw 'Expected exactly one existing static NAT and reserved EIP in Terraform state'
        }
        $natId = [string]$natRows[0].values.id
        $allocation = [string]$eipRows[0].values.id
        $liveNat = Read-AwsJson -Arguments @('ec2', 'describe-nat-gateways', '--nat-gateway-ids', $natId)
        $matches = @($liveNat.NatGateways)
        if ($matches.Count -ne 1 -or $matches[0].State -ne 'available') {
            throw 'Static NAT must currently be available'
        }
        if (@($matches[0].NatGatewayAddresses | Where-Object { $_.AllocationId -eq $allocation }).Count -ne 1) {
            throw 'Live NAT does not use the reserved Terraform-managed EIP'
        }
        $routeState = @($resources | Where-Object {
            $_.address -in @('aws_route_table.application[0]', 'aws_route_table.application[1]')
        })
        if ($routeState.Count -ne 2) { throw 'Expected two application route tables' }
        $routeIds = @($routeState | ForEach-Object { [string]$_.values.id })
        $liveRoutes = Read-AwsJson -Arguments (@('ec2', 'describe-route-tables', '--route-table-ids') + $routeIds)
        if (@($liveRoutes.RouteTables).Count -ne 2) { throw 'Application route table count changed' }
        foreach ($table in $liveRoutes.RouteTables) {
            $default = @($table.Routes | Where-Object { $_.DestinationCidrBlock -eq '0.0.0.0/0' })
            if ($default.Count -ne 1 -or $default[0].NatGatewayId -ne $natId -or
                $default[0].State -ne 'active') {
                throw "Unexpected default route in $($table.RouteTableId)"
            }
            if (@($table.Routes | Where-Object { $_.DestinationPrefixListId }).Count -lt 1) {
                throw "S3 gateway endpoint route missing in $($table.RouteTableId)"
            }
        }
        foreach ($entry in @(
            @{ Name = 'ecommerce-demo-phase5-morning'; Cron = 'cron(0 8 * * ? *)' },
            @{ Name = 'ecommerce-demo-phase5-afternoon'; Cron = 'cron(0 16 * * ? *)' },
            @{ Name = 'ecommerce-demo-phase5-lifecycle'; Cron = 'cron(0 22 * * ? *)' }
        )) {
            $schedule = Read-AwsJson -Arguments @('scheduler', 'get-schedule', '--name', $entry.Name)
            if ($schedule.State -ne 'ENABLED' -or
                $schedule.ScheduleExpression -ne $entry.Cron -or
                $schedule.ScheduleExpressionTimezone -ne 'Europe/Berlin') {
                throw "A live Shopify schedule differs: $($entry.Name)"
            }
        }
        Write-Host "Verified read-only AWS baseline: $natId, $($routeIds -join ', '), existing Shopify schedules."
        Write-Host 'Generating exact, temporary on-demand NAT Terraform plan...'
        & terraform plan -input=false -refresh=true -no-color "-var-file=$varsPath" "-out=$planPath"
        Check-Exit 'terraform plan'
        $planText = & terraform show -json $planPath
        Check-Exit 'terraform show plan JSON'
        [IO.File]::WriteAllText(
            $planJson, ($planText | Out-String),
            (New-Object System.Text.UTF8Encoding($false))
        )
        & python -X utf8 $Guard review --plan $planJson
        Check-Exit 'Strict NAT-only Terraform change review'
        & terraform show -no-color $planPath
        Check-Exit 'Show final saved plan'
        Write-Host ''
        Write-Host 'PLAN VALIDATED. No Terraform apply, AWS resource change, or NAT deletion performed.'
        Write-Host 'This plan is intentionally temporary and will be deleted after review.'
        Write-Host 'CUTOVER REQUIRES A SEPARATE, REVIEWED ROLLBACK PROCEDURE AND IDLE WINDOW.'
    }
    finally { Pop-Location }
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $Temp -Recurse -Force -ErrorAction SilentlyContinue
}
