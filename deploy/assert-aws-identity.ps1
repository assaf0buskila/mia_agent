# Fails unless this shell can call AWS STS. Prints the 12-digit Account only.
# Does not read laptop env files. Does not create VPC/RDS/ECS. Region is pinned
# so ACM and the ALB cannot be created in a different region by accident.

Set-StrictMode -Version 1.0
$ErrorActionPreference = "Stop"

$env:AWS_DEFAULT_REGION = "eu-north-1"

if (-not (Get-Command aws -ErrorAction SilentlyContinue)) {
    Write-Host "aws is not on PATH. Install the current-user MSI (no UAC):"
    Write-Host "  msiexec.exe /i https://awscli.amazonaws.com/AWSCLIV2-User.msi /qn"
    Write-Host "Then open a new PowerShell."
    exit 1
}

$prev = $ErrorActionPreference
$ErrorActionPreference = "Continue"
$raw = & aws sts get-caller-identity --query Account --output text --region eu-north-1 2>$null
$code = $LASTEXITCODE
$ErrorActionPreference = $prev
if ($code -ne 0) {
    Write-Host "No AWS credentials. In this PowerShell run:"
    Write-Host '  $env:AWS_DEFAULT_REGION = "eu-north-1"'
    Write-Host "  aws login"
    Write-Host "  powershell -File deploy/assert-aws-identity.ps1"
    Write-Host "Console user: aws login. Identity Center: aws configure sso then aws sso login. Never paste keys in chat."
    exit 1
}

$account = ("$raw").Trim()
if ($account -notmatch '^\d{12}$') {
    Write-Host "sts did not return a 12-digit account id."
    exit 1
}

Write-Host $account
exit 0
