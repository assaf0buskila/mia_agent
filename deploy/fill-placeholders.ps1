# Copies deploy/*.example.json into deploy/local/ with ACCOUNT_ID / REGION / VPC / subnet / SG tokens replaced.
# Does not call AWS. Does not read laptop env files. Does not copy mia-prod.secret.example.json.
# After this, follow docs/PRODUCTION_BUILD.md section 3 using file://./deploy/local/<name>.json
# (./ is required — file://deploy/... is a host named deploy).

param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d{12}$')]
    [string]$AccountId,

    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[a-z]{2}-[a-z]+-\d+$')]
    [string]$Region,

    [string]$VpcId = "",
    [string]$SubnetPublicA = "",
    [string]$SubnetPublicB = "",
    [string]$SubnetPrivateA = "",
    [string]$SubnetPrivateB = "",
    [string]$SgAlb = "",
    [string]$SgTasks = "",
    [string]$SgRds = "",
    [string]$AlbHash = "",
    [string]$TargetGroupHash = "",
    [string]$CertId = "",
    [string]$Route53ZoneId = "",
    [string]$AlbCanonicalHostedZoneId = "",
    [string]$AlbDnsName = ""
)

Set-StrictMode -Version 1.0
$ErrorActionPreference = "Stop"

$deploy = $PSScriptRoot
$local = Join-Path $deploy "local"
New-Item -ItemType Directory -Force -Path $local | Out-Null

$pairs = @(
    @{ Token = "subnet-PRIVATE_A"; Value = $SubnetPrivateA },
    @{ Token = "subnet-PRIVATE_B"; Value = $SubnetPrivateB },
    @{ Token = "subnet-PUBLIC_A"; Value = $SubnetPublicA },
    @{ Token = "subnet-PUBLIC_B"; Value = $SubnetPublicB },
    @{ Token = "sg-MIA_TASKS"; Value = $SgTasks },
    @{ Token = "sg-MIA_ALB"; Value = $SgAlb },
    @{ Token = "sg-MIA_RDS"; Value = $SgRds },
    @{ Token = "vpc-VPC_ID"; Value = $VpcId },
    @{ Token = "ALB_CANONICAL_HOSTED_ZONE_ID"; Value = $AlbCanonicalHostedZoneId },
    @{ Token = "ROUTE53_ZONE_ID"; Value = $Route53ZoneId },
    @{ Token = "ALB_DNS_NAME"; Value = $AlbDnsName },
    @{ Token = "CERT_ID"; Value = $CertId },
    @{ Token = "REGION"; Value = $Region }
)

$files = Get-ChildItem -Path $deploy -Filter "*.example.json" |
    Where-Object { $_.Name -ne "mia-prod.secret.example.json" }

$count = 0
foreach ($file in $files) {
    $text = [System.IO.File]::ReadAllText($file.FullName)
    # ACCOUNT_ID is an AWS account token, not a substring of secret key names
    # (MIA_INSTAGRAM_ACCOUNT_ID / MIA_META_ADS_ACCOUNT_ID).
    if ($AccountId) {
        $text = [regex]::Replace($text, '(?<![A-Z0-9_])ACCOUNT_ID(?![A-Z0-9_])', $AccountId)
    }
    foreach ($pair in $pairs) {
        if ($pair.Value) {
            $text = $text.Replace($pair.Token, $pair.Value)
        }
    }
    if ($TargetGroupHash) {
        $text = $text.Replace("targetgroup/mia/HASH", "targetgroup/mia/$TargetGroupHash")
    }
    if ($AlbHash) {
        $text = $text.Replace("loadbalancer/app/mia/HASH", "loadbalancer/app/mia/$AlbHash")
        $text = $text.Replace("app/mia/HASH", "app/mia/$AlbHash")
    }
    $outName = $file.Name -replace "\.example\.json$", ".json"
    $outPath = Join-Path $local $outName
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($outPath, $text, $utf8)
    $count++
}

Write-Host "Wrote $count files to deploy/local. Do not commit. Do not copy secrets into this folder."
Write-Host "Fill the Secrets Manager box separately (deploy/mia-prod.secret.example.json locally, then delete)."
Write-Host "Next: docs/PRODUCTION_BUILD.md section 3 with file://./deploy/local/<file>.json from repo root."
