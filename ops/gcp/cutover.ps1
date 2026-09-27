<#
Move Mia's production data and traffic from AWS (eu-north-1) to the GCP VM.

  -Step Rehearse  Dump RDS while AWS keeps serving, restore it on the VM, report tables.
                  Nothing on AWS changes except a short one-off dump task. Safe to repeat.
  -Step Cutover   Freeze AWS (ECS service to 0, mia-* schedules disabled), dump, restore,
                  point mia.assafweb.com at the VM, enable HTTPS, resume jobs, check health.
                  Mia is offline for roughly 10-20 minutes. Telegram retries its webhooks,
                  so owner messages sent meanwhile arrive afterwards.
  -Step Rollback  DNS back to the AWS load balancer, AWS unfrozen, VM jobs paused.
                  Anything written on the VM after cutover is NOT copied back.

The dump runs as a one-off Fargate task inside Mia's VPC (RDS is private). It uploads to a
private S3 bucket `mia-migration-<account>`; this script downloads it, copies it to the VM,
and deletes the local copy. Remove the bucket, role and task definition after AWS teardown.

Usage: .\ops\gcp\cutover.ps1 -ProjectId <gcp-project> -Step Rehearse|Cutover|Rollback
#>
param(
    [Parameter(Mandatory = $true)] [string]$ProjectId,
    [Parameter(Mandatory = $true)] [ValidateSet("Rehearse", "Cutover", "Rollback")] [string]$Step,
    [string]$AwsRegion = "eu-north-1",
    [string]$Cluster = "mia",
    [string]$Service = "mia",
    [string]$Hostname = "mia.assafweb.com"
)

. "$PSScriptRoot\common.ps1"
$StateFile = Join-Path $env:LOCALAPPDATA "mia-cutover-state.json"
$Work = Join-Path $env:TEMP "mia-cutover"
New-Item -ItemType Directory -Force -Path $Work | Out-Null
$R = "--region=$AwsRegion"

function Get-State {
    if (Test-Path $StateFile) { return Get-Content -Raw $StateFile | ConvertFrom-Json }
    return [pscustomobject]@{ disabledSchedules = @(); previousRecord = $null; zoneId = $null }
}
function Save-State($state) { Write-Utf8File $StateFile ($state | ConvertTo-Json -Depth 20) }

function Get-VmIp {
    return (& gcloud compute addresses describe mia-ip "--region=$Region" "--project=$ProjectId" --format="value(address)").Trim()
}

# ---------------------------------------------------------------- AWS freeze / unfreeze
function Set-AwsFrozen([bool]$frozen) {
    $state = Get-State
    if ($frozen) {
        Write-Host "Scaling ECS service $Service to 0..."
        Invoke-AwsJson ecs update-service --cluster $Cluster --service $Service --desired-count 0 $R | Out-Null
        & aws ecs wait services-stable --cluster $Cluster --services $Service $R | Out-Host
        $disabled = @()
        foreach ($s in @((Invoke-AwsJson scheduler list-schedules --name-prefix mia $R).Schedules)) {
            if ($s.State -ne "ENABLED") { continue }
            Set-ScheduleState $s.Name $s.GroupName "DISABLED"
            $disabled += [pscustomobject]@{ name = $s.Name; group = $s.GroupName }
        }
        $state.disabledSchedules = $disabled
        Save-State $state
        Write-Host "AWS frozen (schedules disabled: $(($disabled | ForEach-Object name) -join ', '))."
    } else {
        foreach ($s in @($state.disabledSchedules)) { Set-ScheduleState $s.name $s.group "ENABLED" }
        Write-Host "Scaling ECS service $Service back to 1..."
        Invoke-AwsJson ecs update-service --cluster $Cluster --service $Service --desired-count 1 $R | Out-Null
        & aws ecs wait services-stable --cluster $Cluster --services $Service $R | Out-Host
        $state.disabledSchedules = @()
        Save-State $state
        Write-Host "AWS unfrozen."
    }
}

function Set-ScheduleState([string]$name, [string]$group, [string]$newState) {
    $full = Invoke-AwsJson scheduler get-schedule --name $name --group-name $group $R
    $body = [ordered]@{ Name = $full.Name; GroupName = $full.GroupName; State = $newState
        ScheduleExpression = $full.ScheduleExpression; FlexibleTimeWindow = $full.FlexibleTimeWindow
        Target = $full.Target }
    foreach ($optional in "ScheduleExpressionTimezone", "Description", "StartDate", "EndDate", "KmsKeyArn", "ActionAfterCompletion") {
        if ($full.PSObject.Properties[$optional] -and $null -ne $full.$optional) { $body[$optional] = $full.$optional }
    }
    $file = Join-Path $Work "schedule-$name.json"
    Write-Utf8File $file ($body | ConvertTo-Json -Depth 20)
    Invoke-AwsJson scheduler update-schedule --cli-input-json "file://$file" $R | Out-Null
    Remove-Item $file
    Write-Host "  schedule $name -> $newState"
}

# ---------------------------------------------------------------- dump RDS via Fargate
$DumpScript = @'
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends awscli ca-certificates curl >/dev/null
curl -fsS -o /tmp/rds.pem https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
dsn="$MIA_DATABASE_URL"
dsn="${dsn/postgresql+psycopg:/postgresql:}"
dsn="${dsn/postgres+psycopg:/postgresql:}"
dsn="${dsn//\/etc\/ssl\/certs\/rds-global-bundle.pem/\/tmp\/rds.pem}"
case "$dsn" in
  *sslmode=*) ;;
  *\?*) dsn="$dsn&sslmode=verify-full&sslrootcert=/tmp/rds.pem" ;;
  *) dsn="$dsn?sslmode=verify-full&sslrootcert=/tmp/rds.pem" ;;
esac
pg_dump --format=custom --no-owner --no-privileges --dbname="$dsn" --file=/tmp/mia.dump
echo "mia-dump: $(pg_restore --list /tmp/mia.dump | grep -c 'TABLE DATA') tables, $(stat -c %s /tmp/mia.dump) bytes"
aws s3 cp /tmp/mia.dump "s3://$BUCKET/mia.dump" --only-show-errors \
  --region "$AWS_DEFAULT_REGION" --endpoint-url "https://s3.$AWS_DEFAULT_REGION.amazonaws.com"
echo "mia-dump: uploaded"
'@

function Export-AwsDump {
    $account = (Invoke-AwsJson sts get-caller-identity).Account
    $bucket = "mia-migration-$account"
    & aws s3api head-bucket --bucket $bucket $R *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Creating private bucket $bucket..."
        Invoke-AwsJson s3api create-bucket --bucket $bucket --create-bucket-configuration "LocationConstraint=$AwsRegion" $R | Out-Null
        Invoke-AwsJson s3api put-public-access-block --bucket $bucket --public-access-block-configuration `
            "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true" $R | Out-Null
    }

    $role = "miaMigrationDumpRole"
    $roleArn = "arn:aws:iam::${account}:role/$role"
    & aws iam get-role --role-name $role *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Creating task role $role (write to $bucket only)..."
        $trust = Join-Path $Work "trust.json"
        Write-Utf8File $trust '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'
        Invoke-AwsJson iam create-role --role-name $role --assume-role-policy-document "file://$trust" | Out-Null
        Start-Sleep -Seconds 10
    }
    $policy = Join-Path $Work "policy.json"
    Write-Utf8File $policy ('{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Action":"s3:PutObject","Resource":"arn:aws:s3:::' + $bucket + '/*"}]}')
    Invoke-AwsJson iam put-role-policy --role-name $role --policy-name write-dump --policy-document "file://$policy" | Out-Null

    $svc = (Invoke-AwsJson ecs describe-services --cluster $Cluster --services $Service $R).services[0]
    $td = (Invoke-AwsJson ecs describe-task-definition --task-definition $svc.taskDefinition $R).taskDefinition
    $app = $td.containerDefinitions | Where-Object { $_.name -eq "mia" } | Select-Object -First 1
    $dbSecret = $app.secrets | Where-Object { $_.name -eq "MIA_DATABASE_URL" } | Select-Object -First 1
    if (-not $dbSecret) { throw "the live task definition has no MIA_DATABASE_URL secret" }
    $logGroup = $app.logConfiguration.options.'awslogs-group'

    $dumpTd = [ordered]@{
        family = "mia-dump"; networkMode = "awsvpc"; requiresCompatibilities = @("FARGATE")
        cpu = "512"; memory = "1024"; executionRoleArn = $td.executionRoleArn; taskRoleArn = $roleArn
        containerDefinitions = @([ordered]@{
            name = "dump"; image = "public.ecr.aws/docker/library/postgres:16"; essential = $true
            entryPoint = @("bash", "-c"); command = @($DumpScript -replace "`r", "")
            environment = @(@{ name = "BUCKET"; value = $bucket }, @{ name = "AWS_DEFAULT_REGION"; value = $AwsRegion })
            secrets = @(@{ name = "MIA_DATABASE_URL"; valueFrom = $dbSecret.valueFrom })
            logConfiguration = @{ logDriver = "awslogs"; options = @{
                "awslogs-group" = $logGroup; "awslogs-region" = $AwsRegion; "awslogs-stream-prefix" = "mia-dump" } }
        })
    }
    $tdFile = Join-Path $Work "dump-td.json"
    Write-Utf8File $tdFile ($dumpTd | ConvertTo-Json -Depth 20)
    $dumpArn = (Invoke-AwsJson ecs register-task-definition --cli-input-json "file://$tdFile" $R).taskDefinition.taskDefinitionArn
    Remove-Item $tdFile

    $net = $svc.networkConfiguration.awsvpcConfiguration
    $runFile = Join-Path $Work "run.json"
    Write-Utf8File $runFile ([ordered]@{
        cluster = $Cluster; taskDefinition = $dumpArn; launchType = "FARGATE"; count = 1
        networkConfiguration = @{ awsvpcConfiguration = @{
            subnets = @($net.subnets); securityGroups = @($net.securityGroups); assignPublicIp = $net.assignPublicIp } }
    } | ConvertTo-Json -Depth 20)
    Write-Host "Running the dump task (pulls postgres:16, dumps, uploads; usually 2-4 minutes)..."
    $run = Invoke-AwsJson ecs run-task --cli-input-json "file://$runFile" $R
    Remove-Item $runFile
    if (@($run.failures).Count -gt 0) { throw "run-task failed: $($run.failures | ConvertTo-Json -Compress)" }
    $taskArn = $run.tasks[0].taskArn

    for ($i = 0; $i -lt 3; $i++) {
        & aws ecs wait tasks-stopped --cluster $Cluster --tasks $taskArn $R | Out-Host
        if ($LASTEXITCODE -eq 0) { break }
    }
    $task = (Invoke-AwsJson ecs describe-tasks --cluster $Cluster --tasks $taskArn $R).tasks[0]
    $exit = $task.containers[0].exitCode
    if ($exit -ne 0) {
        throw "dump task failed (exit $exit, $($task.stoppedReason)). Logs: CloudWatch $logGroup, stream prefix mia-dump"
    }

    $local = Join-Path $Work "mia.dump"
    & aws s3 cp "s3://$bucket/mia.dump" $local --only-show-errors $R | Out-Host
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $local)) { throw "download from s3://$bucket failed" }
    Write-Host ("Dump downloaded: {0:N1} MB" -f ((Get-Item $local).Length / 1MB))
    return $local
}

function Import-DumpOnVm([string]$local) {
    try {
        Invoke-Gcloud compute scp $local "${Vm}:/tmp/mia.dump" "--zone=$Zone" "--project=$ProjectId"
    } finally { Remove-Item $local -ErrorAction SilentlyContinue }
    Invoke-OnVm -ProjectId $ProjectId -Command "mia restore /tmp/mia.dump && shred -u /tmp/mia.dump"
}

# ---------------------------------------------------------------- DNS
function Get-Zone {
    $apex = ($Hostname -split "\.", 2)[1]
    $zones = (Invoke-AwsJson route53 list-hosted-zones-by-name --dns-name $apex).HostedZones
    $zone = $zones | Where-Object { $_.Name -eq "$apex." -and -not $_.Config.PrivateZone } | Select-Object -First 1
    if ($zone) { return ($zone.Id -replace '^/hostedzone/', '') }
    return $null
}

function Set-Record($zoneId, $recordSet) {
    $file = Join-Path $Work "dns.json"
    Write-Utf8File $file (@{ Changes = @(@{ Action = "UPSERT"; ResourceRecordSet = $recordSet }) } | ConvertTo-Json -Depth 20)
    Invoke-AwsJson route53 change-resource-record-sets --hosted-zone-id $zoneId --change-batch "file://$file" | Out-Null
    Remove-Item $file
}

function Wait-Dns([string]$ip) {
    Write-Host "Waiting for $Hostname to resolve to $ip..."
    for ($i = 0; $i -lt 60; $i++) {
        $answer = Resolve-DnsName $Hostname -Server 8.8.8.8 -Type A -DnsOnly -ErrorAction SilentlyContinue |
            Where-Object { $_.Type -eq "A" } | ForEach-Object IPAddress
        if ($answer -contains $ip -and @($answer).Count -eq 1) { return }
        Start-Sleep -Seconds 10
    }
    throw "$Hostname still does not resolve to $ip after 10 minutes"
}

function Switch-DnsToVm([string]$ip) {
    $state = Get-State
    $zoneId = Get-Zone
    if ($zoneId) {
        $current = (Invoke-AwsJson route53 list-resource-record-sets --hosted-zone-id $zoneId `
                --start-record-name $Hostname --start-record-type A --max-items 1).ResourceRecordSets |
            Where-Object { $_.Name -eq "$Hostname." -and $_.Type -eq "A" }
        if ($current -and -not $state.previousRecord) { $state.previousRecord = $current }
        $state.zoneId = $zoneId
        Save-State $state
        Set-Record $zoneId @{ Name = $Hostname; Type = "A"; TTL = 60; ResourceRecords = @(@{ Value = $ip }) }
        Write-Host "Route 53: $Hostname -> $ip"
    } else {
        Write-Host ""
        Write-Host "assafweb.com is not hosted in Route 53. At your DNS provider, set:" -ForegroundColor Yellow
        Write-Host "  $Hostname  A  $ip  (TTL 60)" -ForegroundColor Yellow
        Read-Host "Press Enter once saved"
    }
    Wait-Dns $ip
}

# ---------------------------------------------------------------- steps
switch ($Step) {
    "Rehearse" {
        $dump = Export-AwsDump
        Import-DumpOnVm $dump
        Invoke-OnVm -ProjectId $ProjectId -Command "mia status"
        Write-Host "Rehearsal done. AWS is untouched and still serving; the VM holds a copy."
    }
    "Cutover" {
        $ip = Get-VmIp
        Set-AwsFrozen $true
        try {
            $dump = Export-AwsDump
            Import-DumpOnVm $dump
        } catch {
            Write-Warning "Cutover failed before DNS moved; unfreezing AWS. Cause: $_"
            Set-AwsFrozen $false
            throw
        }
        Switch-DnsToVm $ip
        Invoke-OnVm -ProjectId $ProjectId -Command "mia https enable && mia jobs resume"
        for ($i = 0; $i -lt 30; $i++) {
            try {
                $r = Invoke-WebRequest "https://$Hostname/health/ready" -UseBasicParsing -TimeoutSec 15
                if ($r.StatusCode -eq 200) { break }
            } catch { Start-Sleep -Seconds 10 }
        }
        Invoke-OnVm -ProjectId $ProjectId -Command "mia status"
        Write-Host ""
        Write-Host "Mia now runs on GCP. AWS is frozen, not deleted. Rollback: -Step Rollback" -ForegroundColor Green
    }
    "Rollback" {
        $state = Get-State
        Invoke-OnVm -ProjectId $ProjectId -Command "mia jobs pause; mia https disable || true"
        if ($state.zoneId -and $state.previousRecord) {
            $record = $state.previousRecord
            Set-Record $state.zoneId $record
            Write-Host "Route 53: $Hostname restored to the AWS load balancer."
        } else {
            Write-Host "Point $Hostname back at the AWS load balancer at your DNS provider." -ForegroundColor Yellow
        }
        Set-AwsFrozen $false
        Write-Host "Rolled back to AWS. Data written on the VM since cutover was not copied back."
    }
}
