# Mia on Google Cloud

Mia moves from AWS (ECS Fargate + ALB + NAT + RDS in eu-north-1, roughly $105-125/month)
to one small VM in Tel Aviv (`me-west1-a`), built the same way as Leo's. Rough cost: about
₪45/month (VM, 20 GB disk, static IP, snapshots). Model calls are billed by the providers.

## What runs where

| Piece | Where | Replaces on AWS |
| --- | --- | --- |
| VM `mia` | `e2-small`, Ubuntu 24.04, 20 GB, deletion protection on | ECS service + Fargate |
| PostgreSQL 16 | container `postgres`, 127.0.0.1 only, data in `/var/lib/mia/postgres` | RDS `db.t4g.micro` |
| Migrations | container `migrate` (`mia-migrate`) before every start, database `mia` only. `assaf_os_auth` is migrated by hand, see below | ECS migrate override |
| App | container `app`, uvicorn on 127.0.0.1:8000 | ECS task |
| HTTPS | Caddy for `mia.assafweb.com`, automatic certificate | ALB + ACM |
| Due scan / knowledge ingest | systemd timers: every 15 min / hourly | EventBridge Scheduler |
| Settings | Secret Manager `mia-env` (me-west1 only) | Secrets Manager `mia/prod` + task env |
| Database password | `/etc/mia/*.env` on the VM, generated once, never leaves it | RDS-managed secret + Lambda sync |
| Backups | daily disk snapshots kept 7 days, plus `sudo mia backup` (database `mia` only) | RDS backups (14 days) |

Only TCP 80/443 are open to the internet (firewall rule `mia-https`). SSH goes through
`gcloud compute ssh` with OS Login.

`mia-reconcile` is not scheduled: it was DISABLED on AWS and has no console script on master.

## First deploy (fresh start, 2026-09-27)

The AWS account was closed, so Mia starts on GCP with an empty database. Leads already
delivered to the CRM Google Sheet stay there; the knowledge base re-ingests from the site.
`cutover.ps1` (AWS dump and restore) is kept only for the case where AWS is reopened.

Fill the settings file (outside the repo: `..\mia-gcp-settings.env`), then from the
repository root in PowerShell:

```powershell
gcloud auth login
.\ops\gcp\provision.ps1     -ProjectId mia-assafweb
.\ops\gcp\push-settings.ps1 -ProjectId mia-assafweb -EnvFile ..\mia-gcp-settings.env
.\ops\gcp\deploy.ps1        -ProjectId mia-assafweb -Ref gcp-migration
```

Point DNS `mia.assafweb.com` (Vercel) at the VM IP that `provision.ps1` prints: an `A`
record, replacing the old CNAME to the AWS load balancer. Then:

```powershell
gcloud compute ssh mia --zone me-west1-a --project mia-assafweb --command "sudo mia go-live && sudo mia status"
```

`go-live` enables HTTPS, waits for `/health/ready`, resumes the jobs, registers the Telegram
webhook and runs the first knowledge ingest.

### After go-live, check

- `https://mia.assafweb.com/health` shows the deployed `commit_sha`; `/health/ready` is 200.
- A website "Ask Mia" turn on assafweb.com answers.
- An owner Telegram message gets a reply.
- `sudo mia status` shows timers scheduled and `jobs: running`.

## Day to day

| Task | Command |
| --- | --- |
| Deploy a new commit | `.\ops\gcp\deploy.ps1 -ProjectId mia-assafweb` (optionally `-Ref <branch>`) |
| Patch live settings | `.\ops\gcp\push-settings.ps1 -ProjectId mia-assafweb -PatchSecret -Set @{ KEY = "value" }` preserves every other `mia-env` value, then `sudo mia settings` |
| Replace all settings | `.\ops\gcp\push-settings.ps1 -ProjectId mia-assafweb -EnvFile <complete-file>`, then `sudo mia settings` |
| Health and timers | `sudo mia status` |
| Logs | `sudo mia logs app` (or `postgres`, `migrate`, `caddy`) |
| Run a job now | `sudo mia job mia-due-scan` / `sudo mia job mia-ingest-knowledge` |
| Pause / resume jobs | `sudo mia jobs pause` / `sudo mia jobs resume` |
| Database dump | `sudo mia backup` (writes `/var/lib/mia/backups/`, database `mia` only) |

Open a shell with `gcloud compute ssh mia --zone me-west1-a --project mia-assafweb`.

## Owner-session database `assaf_os_auth` (mandatory before a V2-08 image)

The owner OS session tables live in a separate database, `assaf_os_auth`, on the same
PostgreSQL. It was created by `ops/assaf-os/provision_auth.py`. **`mia-migrate` does NOT reach
`assaf_os_auth`.** It migrates only `mia`, and the runtime role `assaf_os_session` cannot alter
tables. A schema change there is a separate step that needs the owner's go, and it runs
**before** the image that needs it. For V2-08 (session renewal), the sequence is:

1. **Backup.** `sudo mia backup` dumps only `mia`. On the VM, also dump `assaf_os_auth`:
   ```sh
   f=/var/lib/mia/backups/assaf_os_auth-$(date -u +%Y%m%dT%H%M%SZ).dump
   sudo docker exec mia-postgres-1 pg_dump -U mia -d assaf_os_auth --format=custom \
     | sudo tee "$f" >/dev/null && sudo chmod 600 "$f" && echo "$f"
   ```
2. **Dry run**, from the repository root with the same gcloud login used for provisioning. It
   changes nothing and must print `"status":"session_chains_dry_run"`:
   ```powershell
   python ops/assaf-os/provision_auth.py --migrate-session-chains
   ```
3. **Apply, only with the owner's explicit go.** It applies
   `migrations/20261015_owner_os_session_chains.sql` to `assaf_os_auth` as the owner, in one
   transaction. Each constraint is added only if absent, so running it again changes nothing.
   It changes no grants.
   ```powershell
   python ops/assaf-os/provision_auth.py --migrate-session-chains --apply
   ```
4. **Verify.** It must print `"status":"session_chains_applied"` and `"role_posture":"pass"`.
   That means the 7 nullable columns and 2 validated constraints are present, and the runtime
   role's posture check passed. On `"status":"failed"` (only a reason code is printed), stop:
   do not deploy the image.
5. **Only then deploy** the image (`deploy.ps1`). The columns are additive and nullable, so the
   image already running is unaffected. A new image without them fails closed with
   `503 owner_auth_unavailable`. It never fails open.
6. **Smoke-check owner authentication** after the deploy. `$OS_ORIGIN` is the configured
   `MIA_OWNER_OS_ORIGIN`:
   ```powershell
   curl.exe -s -o NUL -w "%{http_code}\n" -H "Origin: $OS_ORIGIN" https://mia.assafweb.com/v1/owner/auth/session
   curl.exe -s -D - -o NUL -X POST -H "Origin: $OS_ORIGIN" https://mia.assafweb.com/v1/owner/auth/refresh
   ```
   The unauthenticated session read must print `401`. The refresh without a cookie must answer
   `401`, with a `Set-Cookie` that deletes `__Host-mia-owner` (`Max-Age=0`) and no
   `Server-Timing` header. A `503` means step 3 did not run. Run it, or redeploy the previous
   SHA.

## Known gotchas

- **A new VM starts with jobs paused**, so it cannot send reminders while AWS still runs.
  `cutover.ps1 -Step Cutover` resumes them; do it by hand only if you skip that script.
- **HTTPS waits for DNS.** `mia https enable` only after `mia.assafweb.com` resolves to the
  VM, or Caddy's certificate request fails and backs off.
- **`$` in settings** is quoted by `mia settings` so Compose does not interpolate it.
- **Use `-PatchSecret` for one-key changes.** `-EnvFile` and the historical `-FromAws`
  modes replace the complete secret payload; a partial file would discard other settings.
- **Line endings.** `.gitattributes` keeps `ops/gcp/vm/*` LF; the VM runs them with bash.
- **The first deploy builds on the VM** (a few minutes). The VM keeps the last three builds.
- **Telegram caches the webhook IP.** After a DNS move, Telegram kept delivering to the old AWS address (`last_error_message: Connection timed out`). Fix: re-set the webhook with `ip_address=34.165.183.186` (see `..\\mia-telegram-check.sh` next to the repo).
