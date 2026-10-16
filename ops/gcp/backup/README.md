# Off-host database backup and restore drill (V2-10)

Status: **code only, not activated.** Nothing here runs on its own. Creating the bucket, the
service-account permissions, the key secret, installing the timer on the VM and the first drill
each need Assaf's explicit go. Evidence level today: `CODE_CHECKED` and `LOCAL_TESTED` (fakes, plus
a rehearsal against a real PostgreSQL 16 container). Not `STAGING_VERIFIED`, not `DEPLOYED`.

## What it does

Every 15 minutes the VM takes one consistent `pg_dump -Fc` of database `mia`, checks that
`pg_restore --list` can read it, encrypts it on the VM, proves the ciphertext decrypts back to the
same bytes, deletes the plaintext, and uploads ciphertext plus a manifest to a private bucket. The
manifest is uploaded last, so an object without one is an unfinished backup. The first success of
each UTC day is uploaded a second time under `daily/`.

```
VM, every 15 min                                       Assaf's PC, by hand, never scheduled
mia-backup-offsite.timer -> backup_offsite.py          restore_drill.py
  key   <- Secret Manager mia-backup-key (in memory)     key   <- offline copy Assaf supplies now
  dump  <- docker compose exec postgres pg_dump          fetch <- bucket (or a folder)
  seal  -> openssl aes-256-cbc, pbkdf2 600k, sha256      check <- key fingerprint, both sha256s
  put   -> gs://<bucket>/15min/  (+ daily/ once a day)   restore -> mia_drill_<UTC time> only
                                                         check <- tables, migrations, grants, assaf_os
                                                         drop the scratch database, delete plaintext
```

## Owner decisions this implements (issue 118, 2026-10-15)

| Decision | How |
| --- | --- |
| Private GCS bucket, `me-west1` | `provision_backup.py`: uniform access, public access prevented |
| Every 15 minutes (RPO 15 min) | `mia-backup-offsite.timer`, `OnCalendar=*:5/15` (:05 :20 :35 :50) |
| 15 min dumps kept 7 days, daily dumps kept 90 days | prefixes `15min/` and `daily/` plus `lifecycle.json` |
| Client-side encryption, key in Secret Manager plus an offline copy | one 256-bit key; `mia-backup-key` secret, offline file Assaf holds |
| sha256 recorded at upload | in the manifest and as object metadata `ciphertext_sha256` |
| Restore drill proves the restore | `restore_drill.py`, report with durations against the 2 h RTO |
| Activation is owner-gated | `provision_backup.py` is plan-only by default; `install_on_vm.py` does not enable the timer |

## Files

| File | Runs on | Purpose |
| --- | --- | --- |
| `backup_offsite.py`, `backup_lib.py` | VM | the backup, `--status` for the age of the last good one |
| `ledger_posture.py` | VM and PC | re-own and re-grant schema `assaf_os` after a restore, then verify |
| `mia-backup-offsite.service`, `.timer` | VM | systemd units |
| `install_on_vm.py` | VM, as root | copies the above into place, never enables the timer unless told |
| `provision_backup.py` | PC | bucket, lifecycle, IAM, secret, key. `plan` is the default and runs nothing |
| `restore_drill.py` | PC | the drill. It is not installed on the VM and has no timer |
| `lifecycle.json` | PC | the retention rules |

Everything is standard library Python, LF line endings. The tests are `tests/ops/` and use fakes
for docker and gcloud; openssl is real.

## Activation checklist (each numbered step needs Assaf's go)

0. Read-only preflight on the VM: `gcloud --version` (if it is the snap it lives in `/snap/bin`,
   which the service's `PATH` includes), `python3 --version`, `openssl version`,
   `df -h /var/lib/mia` (a run needs about three times the dump size free), and `sudo mia backup`
   still works. `install_on_vm.py` also refuses when a tool is missing. Do not go on if any fails.
1. Make the key, on the PC, written straight to the offline device:
   `python ops/gcp/backup/provision_backup.py generate-key --out <offline path>\mia-backup.key`.
   It prints the key id, never the key. Make a second offline copy. **Losing every copy of the key
   and the secret makes every backup unreadable.**
2. Review `python ops/gcp/backup/provision_backup.py plan --project mia-assafweb`, then run
   `apply --project mia-assafweb --key-file <offline path>\mia-backup.key`. It is idempotent: it
   enables the two APIs, creates the bucket, sets the lifecycle, lets `mia-vm` add and read
   objects (not delete or overwrite them), creates the regional secret and its reader binding, and
   stores the key through stdin. It never replaces an existing key version.
3. Get the files on the VM through a normal approved deploy of a commit that contains
   `ops/gcp/backup/`; they land in `/opt/mia/src/<tag>/ops/gcp/backup/`.
4. On the VM: `sudo python3 /opt/mia/src/<tag>/ops/gcp/backup/install_on_vm.py --bucket <bucket>
   --project mia-assafweb`. It writes only under `/opt/mia/backup`, `/etc/systemd/system`,
   `/etc/mia/backup-offsite.env` and `/var/lib/mia/backup-offsite`, then `daemon-reload`.
5. One supervised run: `sudo systemctl start mia-backup-offsite.service`, then
   `journalctl -u mia-backup-offsite.service -n 20 --no-pager` (expect `ok object=...`) and
   `gcloud storage objects describe gs://<bucket>/15min/<object>.dump.enc --format=json`.
   **Confirm the output shows `ciphertext_sha256` custom metadata**: the code only compares it when
   gcloud reports it, and its output field name is the one thing not proven off the VM.
6. First drill from the PC against that object (below). Record the report in `HANDOFF.md`.
7. Only then: `sudo python3 .../install_on_vm.py --bucket <bucket> --project mia-assafweb
   --enable-timer`.

Rollback: `sudo systemctl disable --now mia-backup-offsite.timer`, remove the two units and
`/opt/mia/backup`, `systemctl daemon-reload`. The bucket and secret are left for Assaf to delete.

## Watching it

- `sudo python3 /opt/mia/backup/backup_offsite.py --status` prints the age of the last good backup
  as JSON and exits 1 when it is older than 45 minutes (three missed slots). It only reads the
  state file, so it needs no settings and calls nothing.
- `systemctl list-timers 'mia-backup*'`, `systemctl --failed`, `journalctl -u mia-backup-offsite.service`.
- Logs hold reason codes only (`key_unavailable`, `dump_failed`, `upload_unverified`, ...).
  No key, no dump content, no provider payload.
- **There is no alert yet.** A failing backup is visible only by looking. Wiring `--status` into
  the health alert is a follow-up; until then check weekly and after every deploy.

## The restore drill

It restores a real backup into `mia_drill_<UTC timestamp>` on a scratch server and nowhere else,
then drops it. It refuses: any other database name, a scratch server that is not this machine, and
a server that has a database called `mia`. It starts no service and talks to no VM.

```powershell
docker run -d --name mia-drill-pg -e POSTGRES_PASSWORD=drill -p 127.0.0.1:55432:5432 postgres:16
python ops/gcp/backup/restore_drill.py `
  --key-file D:\offline\mia-backup.key --scratch-container mia-drill-pg `
  --latest --bucket mia-assafweb-mia-backups --project mia-assafweb `
  --openssl "C:\Program Files\Git\mingw64\bin\openssl.exe" --report drill.json
docker rm -f mia-drill-pg
```

`--from-dir <folder>` restores a pair you already downloaded; `--prefix daily/` picks from the
daily copies. Use `--scratch-url postgresql://postgres:drill@127.0.0.1:55432/postgres` with
local `psql` and `pg_restore` instead of a container.

Exit codes: 0 passed, 1 a check failed, 2 bad arguments, 3 refused (nothing was changed).
The report lists each check. In order: key valid, manifest valid, key fingerprint matches the
backup, ciphertext sha256 and size, decrypt, plaintext sha256, dump readable by `pg_restore`,
no grant to PUBLIC in the dump (the default `USAGE` on `public` is allowed), scratch database
created, restore (`--no-owner --no-privileges --exit-on-error`, like `mia restore`), restored
table count equals the dump's, `schema_migrations` has rows, no PUBLIC grant on restored
relations, the ledger checks below, scratch database dropped. With `--latest` it also fails if
the newest backup is older than 45 minutes. The whole run must finish inside the 2 hour RTO.

Run it before the timer is enabled, then monthly, and after any key change or schema migration.
Record the report in `HANDOFF.md` under the SHA it was run at. RTO caveat: the drill times a
restore on the PC, not the VM. A real recovery adds the SSH, upload and `mia restore` time on an
`e2-small`.

## Ledger schema `assaf_os` (V2-14 / V2-15): a restore must re-own and re-grant it

`pg_restore --no-owner --no-privileges` (used by `mia restore` and by the drill) leaves a restored
`assaf_os` owned by the restoring role with default ACLs, so its functions are executable by
PUBLIC and the ledger job refuses to run (`ledger_schema_owner_unsafe`). `ledger_posture.py`:

1. re-owns the schema and every table, view, sequence, function, procedure and enum/domain type
   in it to `assaf_os_owner`;
2. revokes everything from PUBLIC;
3. re-applies the reviewed grant set, a file containing only `GRANT`, `REVOKE` and
   `ALTER DEFAULT PRIVILEGES` statements that name `assaf_os` (V2-15's `provision_ledger.py`
   prints the exact set). Anything else in that file is refused, and no quoting or `$` is allowed;
4. checks the result and fails unless: the schema and objects are owned by `assaf_os_owner`; nothing
   is granted to PUBLIC; no grantee other than `assaf_os_owner` and `assaf_os_ledger` holds
   anything, columns included; and `assaf_os_ledger` holds no `DELETE` or `TRUNCATE`.

Steps 1 to 3 are one transaction: all or nothing. The drill does this on every restore that contains
the schema and **fails if the schema is there and no `--ledger-grants-file` is given**, because
the posture could not be proven. A dump without `assaf_os` passes with the schema recorded as
absent. The drill creates `assaf_os_owner` and `assaf_os_ledger` as `NOLOGIN` roles on the scratch
server (a dump does not carry roles); production never creates them here.

After any real `sudo mia restore <dump>` on the VM, before trusting the ledger:

```bash
python3 /opt/mia/backup/ledger_posture.py plan   --grants-file /opt/mia/ledger-grants.sql
sudo python3 /opt/mia/backup/ledger_posture.py apply  --grants-file /opt/mia/ledger-grants.sql
sudo python3 /opt/mia/backup/ledger_posture.py verify
```

`apply` prints a JSON report and exits 1 if the posture is not clean. On a database without the
schema it passes and changes nothing.

Not done yet, and needed before any ledger activation:
- `ops/gcp/vm/mia` `restore` does not call `ledger_posture.py` (that file is being edited by the
  ledger work). Until it does, the commands above are a manual step after every restore.
- V2-15's exact grant-map validator does not exist yet. The posture check here is a floor under it.
  When V2-15 lands, run its validator after `apply` and in the drill, and add it as a check.

## A real recovery from these backups

1. Pause the timer so a half-restored database is not backed up: `sudo systemctl stop
   mia-backup-offsite.timer`.
2. Download the newest pair: `gcloud storage cp gs://<bucket>/15min/mia-<stamp>.dump.enc
   gs://<bucket>/15min/mia-<stamp>.manifest.json .`. Check the manifest's `ciphertext_sha256`.
3. Decrypt with the offline key (iterations are in the manifest, today 600000):
   `openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -md sha256 -pass file:mia-backup.key -in
   mia-<stamp>.dump.enc -out mia.dump`. Check `plaintext_sha256`, then `head -c5 mia.dump` is `PGDMP`.
4. Copy it to the VM, `sudo mia restore mia.dump` (it saves the current database first), then the
   ledger commands above, then `sudo systemctl start mia-backup-offsite.timer`.

## Limits worth knowing

- **Encryption is AES-256-CBC through `openssl enc`.** It is confidential but not authenticated:
  integrity rests on the sha256 values, the manifest and `pg_restore` reading the dump. The VM's
  service account can only add and read objects, so it can not overwrite a backup; anyone who can
  write the bucket and read the key could still forge one. The key fingerprint (`key_id`) names a
  key without revealing it.
- **The VM can read the key.** Anyone who controls the VM or `mia-vm` can decrypt every backup
  and read the database anyway. The offline copy exists for recovery, not for secrecy from the VM.
- **Do not rotate the key casually.** Old backups need the old key; `apply` never replaces a version.
- The bucket has default soft delete (7 days) and no retention lock. A retention lock would conflict
  with the 7 day lifecycle on `15min/`; it is an option for `daily/` only.
- Disk: a run holds the dump, its ciphertext and a verification copy at once, under
  `/var/lib/mia/backup-offsite/runs`, removed afterwards. A crashed run's leftovers are pruned after
  two hours; nothing else is touched.
- Cost at this size is the owner's estimate, about $0.15 to $0.20 per month.
- WAL archiving (point in time recovery) is out of scope; this gives a 15 minute RPO.
