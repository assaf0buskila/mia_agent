<#
Upload Mia's settings to Secret Manager (secret `mia-env`). It prints key names only.

-FromAws copies production exactly as it runs today: the plain environment of the live ECS
task definition `mia` plus every secret it maps from Secrets Manager `mia/prod`. Values go
straight from the AWS CLI into gcloud in memory; nothing is written to disk or printed.
-EnvFile uploads a KEY=value file instead. -Set adds or overrides keys ($null removes one).

Dropped, because the VM owns them: MIA_DATABASE_URL, MIA_BUILD_SHA, AWS_*, POSTGRES_*, PG*.

Usage:
  .\ops\gcp\push-settings.ps1 -ProjectId <id> -FromAws
  .\ops\gcp\push-settings.ps1 -ProjectId <id> -FromAws -Set @{ MIA_LOG_LEVEL = "DEBUG" }
  .\ops\gcp\push-settings.ps1 -ProjectId <id> -EnvFile .\prod.env
Then, on a running VM: sudo mia settings
#>
param(
    [Parameter(Mandatory = $true)] [string]$ProjectId,
    [switch]$FromAws,
    [string]$EnvFile,
    [string]$AwsRegion = "eu-north-1",
    [string]$TaskFamily = "mia",
    [hashtable]$Set = @{}
)

. "$PSScriptRoot\common.ps1"

if ($FromAws -eq [bool]$EnvFile) { throw "pass exactly one of -FromAws or -EnvFile" }
$VmOwned = '^(MIA_DATABASE_URL|MIA_BUILD_SHA|AWS_\w+|POSTGRES_\w+|PG\w+)$'
$settings = [ordered]@{}

if ($FromAws) {
    # The revision the live service runs, not merely the newest registered one.
    $service = (Invoke-AwsJson ecs describe-services --cluster $TaskFamily --services $TaskFamily --region $AwsRegion).services | Select-Object -First 1
    $taskDefinition = if ($service -and $service.taskDefinition) { $service.taskDefinition } else { $TaskFamily }
    $td = (Invoke-AwsJson ecs describe-task-definition --task-definition $taskDefinition --region $AwsRegion).taskDefinition
    $container = $td.containerDefinitions | Where-Object { $_.name -eq "mia" } | Select-Object -First 1
    if (-not $container) { throw "task definition $TaskFamily has no container named mia" }
    Write-Host "Reading $($td.family):$($td.revision) from $AwsRegion"
    foreach ($entry in @($container.environment)) { $settings[$entry.name] = [string]$entry.value }

    # valueFrom looks like arn:aws:secretsmanager:<region>:<acct>:secret:<id>:<json-key>::
    $bySecret = @{}
    foreach ($entry in @($container.secrets)) {
        if ($entry.valueFrom -notmatch '^(arn:aws:secretsmanager:[^:]+:\d+:secret:[^:]+):([^:]*):') {
            throw "unsupported secret reference for $($entry.name)"
        }
        $secretArn = $Matches[1]; $jsonKey = $Matches[2]
        if (-not $bySecret.ContainsKey($secretArn)) { $bySecret[$secretArn] = @() }
        $bySecret[$secretArn] += , @($entry.name, $jsonKey)
    }
    foreach ($secretArn in $bySecret.Keys) {
        $raw = (Invoke-AwsJson secretsmanager get-secret-value --secret-id $secretArn --region $AwsRegion).SecretString
        $json = $raw | ConvertFrom-Json
        foreach ($pair in $bySecret[$secretArn]) {
            $name = $pair[0]; $key = if ($pair[1]) { $pair[1] } else { $name }
            $property = $json.PSObject.Properties[$key]
            if ($null -eq $property) { throw "secret has no key $key (for $name)" }
            $settings[$name] = [string]$property.Value
        }
    }
} else {
    if (-not (Test-Path -LiteralPath $EnvFile)) { throw "settings file not found: $EnvFile" }
    foreach ($raw in Get-Content -LiteralPath $EnvFile -Encoding UTF8) {
        $line = $raw -replace '^\s*export\s+', ''
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') { $settings[$Matches[1]] = $Matches[2] }
    }
}

foreach ($entry in $Set.GetEnumerator()) {
    if ($null -eq $entry.Value) { $settings.Remove($entry.Key) } else { $settings[$entry.Key] = [string]$entry.Value }
}

$lines = [System.Collections.Generic.List[string]]::new()
$dropped = @()
foreach ($name in @($settings.Keys)) {
    if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw "invalid setting name" }
    if ($name -match $VmOwned) { $dropped += $name; continue }
    $value = [string]$settings[$name]
    if ($value -match "[`r`n`0]") { throw "$name must be single-line text" }
    $lines.Add("$name=$value")
}

foreach ($required in "MIA_ENV", "MIA_PUBLIC_BASE_URL", "MIA_TELEGRAM_BOT_TOKEN") {
    if (-not ($lines | Where-Object { $_.StartsWith("$required=") })) { throw "$required is missing from settings" }
}

Write-Host "Uploading $($lines.Count) settings (VM-owned, dropped: $($dropped -join ', ')):"
$lines | ForEach-Object { "  " + $_.Split("=", 2)[0] } | Sort-Object | Write-Host

$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    ($lines -join "`n") | & gcloud secrets versions add mia-env --data-file=- "--project=$ProjectId" *> $null
} finally { $ErrorActionPreference = $previous }
if ($LASTEXITCODE -ne 0) { throw "upload failed (exit $LASTEXITCODE)" }

Write-Host "Uploaded. On a running VM, apply it with: sudo mia settings"
