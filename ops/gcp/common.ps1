# Shared helpers for the ops/gcp scripts. Dot-source it: . "$PSScriptRoot\common.ps1"

$ErrorActionPreference = "Stop"

if (-not (Get-Command gcloud -ErrorAction SilentlyContinue)) {
    $sdk = Join-Path $env:LOCALAPPDATA "Google\Cloud SDK\google-cloud-sdk\bin"
    if (-not (Test-Path (Join-Path $sdk "gcloud.cmd"))) {
        throw "gcloud is not installed: https://cloud.google.com/sdk/docs/install"
    }
    $env:PATH = "$sdk;$env:PATH"
}

$script:Zone = "me-west1-a"
$script:Region = "me-west1"
$script:Vm = "mia"

# Run gcloud and stop on failure. gcloud writes progress to stderr, which Windows
# PowerShell 5.1 would otherwise turn into errors.
function Invoke-Gcloud {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & gcloud @args } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($args -join ' ') failed (exit $LASTEXITCODE)" }
}

# Return whether a gcloud command succeeds, without printing anything.
function Test-Gcloud {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { & gcloud @args *> $null } finally { $ErrorActionPreference = $previous }
    return $LASTEXITCODE -eq 0
}

# Run a command on the VM as root.
function Invoke-OnVm {
    param([Parameter(Mandatory = $true)] [string]$ProjectId, [Parameter(Mandatory = $true)] [string]$Command)
    Invoke-Gcloud compute ssh $script:Vm "--zone=$script:Zone" "--project=$ProjectId" "--command=sudo bash -c '$Command'"
}

# Run the AWS CLI and parse its JSON output. Never echoes the output itself.
function Invoke-AwsJson {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $out = & aws @args --output json 2>$null | Out-String } finally { $ErrorActionPreference = $previous }
    if ($LASTEXITCODE -ne 0) { throw "aws $($args[0..1] -join ' ') failed (exit $LASTEXITCODE)" }
    if ($out.Trim()) { return $out | ConvertFrom-Json }
    return $null
}

# Write UTF-8 without a BOM (the AWS CLI rejects a BOM in file:// input).
function Write-Utf8File {
    param([string]$Path, [string]$Text)
    [IO.File]::WriteAllText($Path, $Text, [Text.UTF8Encoding]::new($false))
}
