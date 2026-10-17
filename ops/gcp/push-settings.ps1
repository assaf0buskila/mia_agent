<#
Upload Mia's settings to Secret Manager (secret `mia-env`). It prints key names only.

-FromAws is historical migration tooling: it imports the plain environment of the live ECS
task definition `mia` plus every secret it maps from Secrets Manager `mia/prod`. Values go
straight from the AWS CLI into gcloud in memory; nothing is written to disk or printed.
-EnvFile uploads a KEY=value file instead. -Set adds or overrides keys ($null removes one).
-PatchSecret reads the current GCP secret directly into memory and merges only -Set. It
preserves every other setting, does not read an env file and never prints secret values.

Dropped, because the VM owns them: MIA_DATABASE_URL, MIA_BUILD_SHA, AWS_*, POSTGRES_*, PG*.

Usage:
  .\ops\gcp\push-settings.ps1 -ProjectId <id> -FromAws
  .\ops\gcp\push-settings.ps1 -ProjectId <id> -FromAws -Set @{ MIA_LOG_LEVEL = "DEBUG" }
  .\ops\gcp\push-settings.ps1 -ProjectId <id> -EnvFile .\prod.env
  .\ops\gcp\push-settings.ps1 -ProjectId <id> -PatchSecret -Set @{ MIA_LOG_LEVEL = "DEBUG" }
Then, on a running VM: sudo mia settings
#>
param(
    [Parameter(Mandatory = $true)] [string]$ProjectId,
    [switch]$FromAws,
    [string]$EnvFile,
    [switch]$PatchSecret,
    [string]$AwsRegion = "eu-north-1",
    [string]$TaskFamily = "mia",
    [hashtable]$Set = @{}
)

. "$PSScriptRoot\common.ps1"
. "$PSScriptRoot\settings-lib.ps1"

if ($PatchSecret -and ($FromAws -or $EnvFile)) {
    throw "-PatchSecret cannot be combined with -FromAws or -EnvFile"
}
if ($PatchSecret -and $Set.Count -eq 0) {
    throw "-PatchSecret requires at least one -Set entry"
}
if (-not $PatchSecret -and ($FromAws -eq [bool]$EnvFile)) {
    throw "pass exactly one of -FromAws or -EnvFile"
}
$settings = [ordered]@{}

if ($PatchSecret) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $current = (& gcloud secrets versions access latest --secret=mia-env "--project=$ProjectId" 2>$null | Out-String)
        $readExit = $LASTEXITCODE
    } finally { $ErrorActionPreference = $previous }
    if ($readExit -ne 0) {
        throw "cannot read the current mia-env secret (exit $readExit)"
    }
    $baselineLines = @(Merge-MiaSettings -Current $current -Set @{})
    $baselineNames = @($baselineLines | ForEach-Object {
        if ($_ -match '^([A-Za-z_][A-Za-z0-9_]*)=') { $Matches[1] }
    })
    foreach ($required in "MIA_ENV", "MIA_PUBLIC_BASE_URL", "MIA_TELEGRAM_BOT_TOKEN") {
        if ($required -notin $baselineNames) {
            throw "current mia-env secret is missing $required"
        }
    }
    $lines = @(Merge-MiaSettings -Current $current -Set $Set)
} elseif ($FromAws) {
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

if (-not $PatchSecret) {
    $current = [System.Collections.Generic.List[string]]::new()
    $dropped = @()
    foreach ($name in @($settings.Keys)) {
        if ($name -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') { throw "invalid setting name" }
        if ($name -match $script:MiaVmOwnedSetting) { $dropped += $name; continue }
        $value = [string]$settings[$name]
        Assert-MiaSettingValue $value
        $current.Add("$name=$value")
    }
    $lines = @(Merge-MiaSettings -Current ($current -join "`n") -Set $Set)
}

$names = @($lines | ForEach-Object { if ($_ -match '^([A-Za-z_][A-Za-z0-9_]*)=') { $Matches[1] } })
foreach ($required in "MIA_ENV", "MIA_PUBLIC_BASE_URL", "MIA_TELEGRAM_BOT_TOKEN") {
    if ($required -notin $names) { throw "$required is missing from settings" }
}

$source = if ($PatchSecret) { "the current secret plus patches" } else { "the selected source" }
Write-Host "Uploading $($names.Count) settings from ${source}:"
$names | Sort-Object | ForEach-Object {
    $suffix = if ($Set.ContainsKey($_)) { " (set)" } else { "" }
    Write-Host "  $_$suffix"
}

$OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$previous = $ErrorActionPreference
$ErrorActionPreference = "Continue"
try {
    ($lines -join "`n") | & gcloud secrets versions add mia-env --data-file=- "--project=$ProjectId" *> $null
} finally { $ErrorActionPreference = $previous }
if ($LASTEXITCODE -ne 0) { throw "upload failed (exit $LASTEXITCODE)" }

Write-Host "Uploaded. On a running VM, apply it with: sudo mia settings"
