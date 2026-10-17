<#
One-time setup of Mia's VM on Google Cloud. Safe to re-run: it skips what already exists.

Creates: the Compute Engine and Secret Manager APIs, a `mia-vm` service account, the
`mia-env` secret (filled by push-settings.ps1), a static IP `mia-ip`, a firewall rule for
TCP 80/443 only, a daily snapshot schedule kept for 7 days, and the VM itself (deletion
protection on), which installs Docker on first boot (vm/bootstrap.sh).

Usage: .\ops\gcp\provision.ps1 -ProjectId <project-id> [-MachineType e2-small]
#>
param(
    [Parameter(Mandatory = $true)] [string]$ProjectId,
    [string]$MachineType = "e2-small"
)

. "$PSScriptRoot\common.ps1"
$ServiceAccount = "mia-vm@$ProjectId.iam.gserviceaccount.com"
$P = "--project=$ProjectId"

Write-Host "Enabling APIs (the first run takes a minute)..."
Invoke-Gcloud services enable compute.googleapis.com secretmanager.googleapis.com $P

if (-not (Test-Gcloud iam service-accounts describe $ServiceAccount $P)) {
    Write-Host "Creating service account mia-vm..."
    Invoke-Gcloud iam service-accounts create mia-vm --display-name="Mia VM" $P
}

if (-not (Test-Gcloud secrets describe mia-env $P)) {
    Write-Host "Creating secret mia-env (stored in $Region only)..."
    Invoke-Gcloud secrets create mia-env --replication-policy=user-managed --locations=$Region $P
}

# A new service account can take a few seconds to become visible to IAM.
for ($attempt = 1; ; $attempt++) {
    if (Test-Gcloud secrets add-iam-policy-binding mia-env `
            --member="serviceAccount:$ServiceAccount" `
            --role=roles/secretmanager.secretAccessor $P) { break }
    if ($attempt -ge 6) { throw "could not grant mia-vm access to the mia-env secret" }
    Start-Sleep -Seconds 10
}

if (-not (Test-Gcloud compute addresses describe mia-ip --region=$Region $P)) {
    Write-Host "Reserving static IP mia-ip..."
    Invoke-Gcloud compute addresses create mia-ip --region=$Region $P
}
$Ip = (& gcloud compute addresses describe mia-ip --region=$Region --format="value(address)" $P).Trim()

if (-not (Test-Gcloud compute firewall-rules describe mia-https $P)) {
    Write-Host "Creating firewall rule mia-https (TCP 80 and 443 only)..."
    Invoke-Gcloud compute firewall-rules create mia-https --network=default `
        --action=allow --direction=INGRESS "--rules=tcp:80,tcp:443" `
        --source-ranges=0.0.0.0/0 --target-tags=mia-https $P
}

if (-not (Test-Gcloud compute resource-policies describe mia-daily --region=$Region $P)) {
    Write-Host "Creating daily snapshot schedule mia-daily..."
    Invoke-Gcloud compute resource-policies create snapshot-schedule mia-daily `
        --region=$Region --daily-schedule --start-time=01:00 --max-retention-days=7 `
        --on-source-disk-delete=keep-auto-snapshots --storage-location=$Region $P
}

if (-not (Test-Gcloud compute instances describe $Vm --zone=$Zone $P)) {
    Write-Host "Creating VM $Vm ($MachineType, $Zone)..."
    Invoke-Gcloud compute instances create $Vm --zone=$Zone --machine-type=$MachineType `
        --image-family=ubuntu-2404-lts-amd64 --image-project=ubuntu-os-cloud `
        --boot-disk-size=20GB --boot-disk-type=pd-balanced `
        --address=$Ip --tags=mia-https --deletion-protection `
        --service-account=$ServiceAccount --scopes=cloud-platform `
        --metadata=enable-oslogin=TRUE `
        --metadata-from-file="startup-script=$PSScriptRoot\vm\bootstrap.sh" `
        --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring `
        --labels=app=mia $P
    Invoke-Gcloud compute disks add-resource-policies $Vm --resource-policies=mia-daily --zone=$Zone $P
}

Write-Host ""
Write-Host "Done. Mia's IP is $Ip (DNS moves here during cutover, not now)."
Write-Host "The VM installs Docker on first boot (about 2 minutes)."
Write-Host "Next: .\ops\gcp\push-settings.ps1 -ProjectId $ProjectId -FromAws"
