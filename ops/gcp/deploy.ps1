<#
Deploy a commit of Mia to the GCP VM. It ships the commit, never uncommitted changes.

Packs the commit with `git archive`, copies it to the VM, and runs `mia deploy <tag> <sha>`
there: build the image, fetch settings, migrate, restart. /health then reports the sha.

Usage: .\ops\gcp\deploy.ps1 -ProjectId <id> [-Ref <commit or branch>]
#>
param(
    [Parameter(Mandatory = $true)] [string]$ProjectId,
    [string]$Ref = "HEAD"
)

. "$PSScriptRoot\common.ps1"

# Run git from the repository root: `git archive` run inside ops/gcp packs only that folder.
$Root = (git -C $PSScriptRoot rev-parse --show-toplevel).Trim()
$Sha = (git -C $Root rev-parse "$Ref^{commit}").Trim()
if ($LASTEXITCODE -ne 0 -or -not $Sha) { throw "unknown ref: $Ref" }
$Tag = $Sha.Substring(0, 12)
if (git -C $Root status --porcelain --untracked-files=no) {
    Write-Warning "Uncommitted changes are not deployed; shipping commit $Tag only."
}

$Archive = Join-Path $env:TEMP "mia-$Tag.tar.gz"
git -C $Root archive --format=tar.gz -o $Archive $Sha
if ($LASTEXITCODE -ne 0) { throw "git archive failed" }

try {
    Write-Host "Copying $Tag to $Vm..."
    Invoke-Gcloud compute scp $Archive "${Vm}:/tmp/mia-$Tag.tar.gz" "--zone=$Zone" "--project=$ProjectId"
} finally {
    Remove-Item $Archive -ErrorAction SilentlyContinue
}

$remote = @(
    "set -e"
    "for i in `$(seq 60); do [ -f /var/lib/mia/.bootstrap-done ] && break; sleep 5; done"
    "rm -rf /opt/mia/src/$Tag && mkdir -p /opt/mia/src/$Tag"
    "tar -xzf /tmp/mia-$Tag.tar.gz -C /opt/mia/src/$Tag && rm /tmp/mia-$Tag.tar.gz"
    "bash /opt/mia/src/$Tag/ops/gcp/vm/mia deploy $Tag $Sha"
) -join "; "

Write-Host "Building and starting $Tag on $Vm (the first build takes several minutes)..."
Invoke-OnVm -ProjectId $ProjectId -Command $remote
