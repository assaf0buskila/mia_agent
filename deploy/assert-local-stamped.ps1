# Fails if deploy/local JSON still has placeholder tokens.
# Does not call AWS. Does not read laptop env files. Does not read secrets.
# -Stage network: after the first fill (VPC/subnets/SGs). HASH may remain.
# -Stage alb: after the second fill (ALB/TG hashes, ACM, Route 53).

param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("network", "alb")]
    [string]$Stage
)

Set-StrictMode -Version 1.0
$ErrorActionPreference = "Stop"

$local = Join-Path $PSScriptRoot "local"
if (-not (Test-Path $local)) {
    throw "deploy/local is missing. Run deploy/fill-placeholders.ps1 first."
}

$forbidden = @(
    "vpc-VPC_ID",
    "subnet-PRIVATE_A",
    "subnet-PRIVATE_B",
    "subnet-PUBLIC_A",
    "subnet-PUBLIC_B",
    "sg-MIA_ALB",
    "sg-MIA_TASKS",
    "sg-MIA_RDS"
)
if ($Stage -eq "alb") {
    $forbidden += @(
        "HASH",
        "CERT_ID",
        "ALB_DNS_NAME",
        "ALB_CANONICAL_HOSTED_ZONE_ID",
        "ROUTE53_ZONE_ID"
    )
}

$files = Get-ChildItem -Path $local -Filter "*.json"
if ($files.Count -eq 0) {
    throw "deploy/local has no JSON. Run deploy/fill-placeholders.ps1 first."
}

$hits = @()
foreach ($file in $files) {
    $text = [System.IO.File]::ReadAllText($file.FullName)
    foreach ($token in $forbidden) {
        if ($text.Contains($token)) {
            $hits += "$($file.Name): $token"
        }
    }
}

if ($hits.Count -gt 0) {
    Write-Host "Unstamped tokens in deploy/local ($Stage):"
    $hits | ForEach-Object { Write-Host "  $_" }
    exit 1
}

Write-Host "deploy/local is stamped for stage $Stage."
exit 0
