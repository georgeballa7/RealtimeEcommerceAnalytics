<#
.SYNOPSIS
    Guarded on-demand NAT cutover; preview by default, explicit idle-only -Apply.
.DESCRIPTION
    Reads existing Terraform state and the live NAT/route/schedule baseline.
    Reconstructs the deployed image digests and optional Phase 6 flags, then
    creates and inspects a temporary Terraform plan. Rejects ANY change beyond
    deletion of one static NAT/two default routes plus creation of the eight
    reviewed on-demand NAT Lambda/IAM/Scheduler components.
    Applies ONLY the exact pre-reviewed plan after idle checks and explicit approval.
.EXAMPLE
    .\scripts\aws\plan_phase7_nat_cutover.ps1
#>
[CmdletBinding()]
param([switch]$Apply)
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
        Write-Host 'STRICT NAT CUTOVER PLAN REVIEWED: 8 creates and 3 deletes (old NAT plus two default routes).'
        if (-not $Apply) {
            Write-Host 'PLAN ONLY. To cut over during an idle window, use this separate script with -Apply.'
            return
        }
        # Deleting the Terraform-owned NAT while an 08:00/16:00/22:00
        # source/Glue/dbt workload is active would break production traffic.
        # Restrict the cutover to the late-morning quiet window in Berlin,
        # when the old EIP can be released before the 15:30 prewarm.
        $tz = [TimeZoneInfo]::FindSystemTimeZoneById('W. Europe Standard Time')
        $local = [TimeZoneInfo]::ConvertTime([DateTimeOffset]::UtcNow, $tz)
        $minutes = $local.Hour * 60 + $local.Minute
        if ($minutes -lt 10 * 60 -or $minutes -gt 12 * 60 + 30) {
            throw 'NAT cutover requires the 10:00–12:30 Europe/Berlin quiet window'
        }
        foreach ($status in @('RUNNING','PENDING')) {
            $ecs = Read-AwsJson -Arguments @('ecs','list-tasks','--cluster','ecommerce-demo','--desired-status',$status)
            if (@($ecs.taskArns | Where-Object { $_ }).Count -gt 0) {
                throw "Refusing NAT cutover: ECS $status tasks still exist"
            }
        }
        $jobs = Read-AwsJson -Arguments @('glue','get-job-runs','--job-name','ecommerce-phase6-shopify-silver-demo','--max-results','10')
        if (@($jobs.JobRuns | Where-Object {
            $_.JobRunState -in @('STARTING','RUNNING','STOPPING','WAITING')
        }).Count -gt 0) { throw 'Refusing NAT cutover: Glue still active' }
        $queue = Read-AwsJson -Arguments @('sqs','get-queue-attributes',
            '--queue-url','https://sqs.eu-central-1.amazonaws.com/825414820081/ecommerce-demo-events',
            '--attribute-names','ApproximateNumberOfMessages','ApproximateNumberOfMessagesNotVisible')
        foreach ($key in @('ApproximateNumberOfMessages','ApproximateNumberOfMessagesNotVisible')) {
            if ([int]$queue.Attributes.$key -gt 0) { throw 'Refusing NAT cutover: SQS backlog' }
        }
        $date = $local.ToString('yyyy-MM-dd')
        $slotKey = Join-Path $Temp 'morning-key.json'
        [IO.File]::WriteAllText($slotKey,'{"id":{"S":"phase6-morning"}}',
                               (New-Object System.Text.UTF8Encoding($false)))
        $slot = Read-AwsJson -Arguments @('dynamodb','get-item',
            '--table-name','ecommerce-demo-phase5-controller',
            '--key',('file://' + ($slotKey -replace '\\','/')),
            '--consistent-read')
        if ($slot.Item.day.S -ne $date -or $slot.Item.phase.S -ne 'completed' -or
            -not $slot.Item.warehouse_verified_at.N -or -not $slot.Item.batch_id.S) {
            throw 'Refusing NAT cutover: the morning Redshift warehouse batch is not verified completed'
        }
        $dbtRecord = 'phase7-dbt:morning:' + [string]$slot.Item.batch_id.S
        $dbtKey = Join-Path $Temp 'dbt-key.json'
        $keyObject = @{ id = @{ S = $dbtRecord } } | ConvertTo-Json -Compress -Depth 4
        [IO.File]::WriteAllText($dbtKey,$keyObject,
                               (New-Object System.Text.UTF8Encoding($false)))
        $dbt = Read-AwsJson -Arguments @('dynamodb','get-item',
            '--table-name','ecommerce-demo-phase5-controller',
            '--key',('file://' + ($dbtKey -replace '\\','/')),
            '--consistent-read')
        if ($dbt.Item.state.S -notin @('succeeded','failed')) {
            throw "Refusing NAT cutover: dbt for $dbtRecord not terminal"
        }
        $loaderKey = Join-Path $Temp 'loader-key.json'
        [IO.File]::WriteAllText($loaderKey,'{"id":{"S":"phase6-redshift-loader"}}',
                               (New-Object System.Text.UTF8Encoding($false)))
        $loader = Read-AwsJson -Arguments @('dynamodb','get-item',
            '--table-name','ecommerce-demo-phase5-controller',
            '--key',('file://' + ($loaderKey -replace '\\','/')),
            '--consistent-read')
        if ($loader.Item) { throw 'Refusing NAT cutover: Redshift loader lease active' }

        Write-Host 'SAFE IDLE WINDOW VERIFIED: warehouse and dbt terminal; no ECS, Glue, SQS or loader work.'
        Write-Host 'Terraform will delete one static NAT and two routes, then install NAT lifecycle resources.'
        Write-Host 'The new controller will begin prewarming at the 15:30 afternoon window.'
        $approval = Read-Host 'Type CUTOVER-NAT to apply the reviewed plan'
        if ($approval -cne 'CUTOVER-NAT') { throw 'Cutover not approved' }
        & terraform apply -input=false $planPath
        Check-Exit 'Apply strictly reviewed NAT cutover'
        $schedule = Read-AwsJson -Arguments @('scheduler','get-schedule',
            '--name','ecommerce-phase6-nat-lifecycle-demo')
        if ($schedule.State -ne 'ENABLED') { throw 'NAT lifecycle schedule not enabled after apply' }
        Write-Host 'ON-DEMAND NAT CUTOVER APPLIED. NAT is now lifecycle-managed.'
        Write-Host 'Check CloudWatch and the 15:30 prewarm for an active route before 16:00 generator.'
    }
    finally { Pop-Location }
}
finally {
    Pop-Location
    Remove-Item -LiteralPath $Temp -Recurse -Force -ErrorAction SilentlyContinue
}
