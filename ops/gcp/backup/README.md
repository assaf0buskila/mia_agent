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
| `ledger_posture.py` | PC (the drill) only | re-own and re-grant schema `assaf_os` on the drill's scratch server, then verify. It has no command line and is never installed on the VM: `mia restore` uses V2-15's `provision_ledger.py --mode reapply`, the only grant path in production |
| `mia-backup-offsite.service`, `.timer` | VM | systemd units |
| `install_on_vm.py` | VM, as root | copies `backup_lib.py`, `backup_offsite.py` and the units into place, never enables the timer unless told |
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
   stores the key through stdin. It never replaces an existing key version. An EXISTING bucket is
   left as it is only after its location (`me-west1`), uniform access and public access prevention
   check out; a mismatch stops `apply` before it changes anything. When a key version already exists
   it prints the id of `--key-file` (a fingerprint, never the key): it must equal the `key_id` in a
   backup manifest, otherwise the offline copy is not the stored key.
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
  No key, no dump content, no provider payload, and not the tail of a tool's stderr either.
- A slot is **skipped** (`skipped code=state_lock_busy`, exit 0, no failure recorded) while `mia restore`,
  `mia deploy` or another state-changing `mia` command holds `/var/lock/mia-state.lock`: a dump taken
  in the middle of a restore is a partial database and would be uploaded as the newest good backup.
  The next slot takes the backup; `--status` goes stale after three missed slots.
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

## A restore strips ownership and grants: what puts them back

`pg_restore --no-owner --no-privileges` (used by `mia restore` and by the drill) leaves every object
owned by the restoring role with default ACLs. On the VM that role is the superuser `mia`, so
without a repair `mia_app` is denied on every table, `assaf_os` loses `assaf_os_owner` and its
grants, and its functions fall back to PUBLIC EXECUTE. V2-14's `_validate_main_role_catalog` does not
look at `public` table access, so a restore that was not repaired still passes it.

### On the VM: `sudo mia restore <dump>` repairs it before the services start

The restore arm runs the repair between `pg_restore` and `start_services`, under the
`/var/lock/mia-state.lock` it already holds (V2-10, `V2_PG_CONTRACTS.md` section 18.1). Any failure
prints `mia: restore stopped, the services stay down: reason=<code>` and exits 1. The services
stay DOWN: `app` and `caddy` were stopped first and `start_services` is never reached. Every step
is idempotent, so after fixing the cause run `sudo mia restore <same dump>` again (it takes a new
safety dump first). The steps, in this order:

| # | Step | Skipped, with a logged reason, when |
|---|---|---|
| 1 | `python3 /opt/mia/app-role-demote/app_role_demote.py --reapply` (V2-14a) | `mia_app` does not exist (`app_role_not_demoted`) |
| 2 | `provision_ledger.py --target mia-vm --mode reapply --apply` (V2-15), in the deployed image | no schema `assaf_os` in the restored database (`ledger_schema_absent`) |
| 3 | V2-14's real `_validate_main_role_catalog`, in the deployed image, on the real main DSN | the main DSN in `runtime.env` is not `mia_app` yet (`main_dsn_not_demoted`) |
| 4 | `owner_os_revocations.revoked_before = now()` and every open owner session closed, in `assaf_os_auth` | no `assaf_os_auth` database (`owner_auth_database_absent`) or none of its tables (`owner_auth_tables_absent`) |
| 5 | a real `SELECT` and an `INSERT` (rolled back) on an application table, as the main DSN role | never |

Details that matter:

- Step 1 runs the helper directly. `app-role-demote.sh` would report the lock busy. The helper files
  are the ones the V2-14a runbook installs in `/opt/mia/app-role-demote/` (the `.py` and
  `sql/app-role-demote.sql`). `--reapply` tolerates `assaf_os` PUBLIC EXECUTE; step 2 clears it.
- Step 2 runs `/opt/mia/src/<MIA_TAG>/ops/assaf-os/provision_ledger.py`, the script deployed with the
  running image, mounted at `/app/ops/assaf-os/provision_ledger.py` into `os-ledger-migrate` as root
  (it reads `/etc/mia/runtime.env`). **`/etc/mia` is mounted read-only (`:ro`):** a restore never
  writes a VM-owned file, and the reapply mode never tries to. The approved SHA is the image's own
  `MIA_BUILD_SHA`. It never changes an existing role's password, re-owns `assaf_os` and its objects to
  `assaf_os_owner`, revokes, re-creates the owner's default ACL, re-applies the exact grants and runs
  V2-15's `validate_ledger_catalog`. It needs `/etc/mia/os-ledger-migrate.env` (the same single key
  `mia os-ledger` accepts). A ledger runtime role that does not exist (a fresh cluster) is created only
  from a DSN already staged in `runtime.env`; with nothing staged the script refuses before it creates
  anything, its transaction rolls back and the restore stops with `ledger_reapply_failed`. Provision it
  explicitly first (`provision_ledger.py --mode provision`, V2-15 runbook), then run `mia restore` again.
- `mia settings` and `mia deploy` drop `MIA_OWNER_OS_LEDGER_*`, `MIA_OS_LEDGER_*` and
  `MIA_APP_ROLE_POSTURE` from `mia-env`: the ledger DSNs and the posture are VM-owned. The other
  `MIA_OWNER_OS_*` settings (enable flags, Google keys, session secret and DSN, origin) still come
  from `mia-env`.
- Step 4 mirrors what a logout-everywhere does, for every owner id that has a session, a login proof
  or a revocation row: `revoked_before` only moves forward (`GREATEST`), and open sessions get
  `revoked_at`. The owner-auth database is not part of this restore, but the data its sessions act on
  just went back in time. It is skipped only when the database or its tables do not exist, because
  then there is no session to revoke. A partial table set stops the restore.
- Step 5 reads the main DSN from `/etc/mia/runtime.env` (only the canonical
  `postgresql://<role>:<hex>@127.0.0.1:5432/mia` form), and passes the password to `psql` on stdin,
  never as an argument. It picks the first application table with no user trigger. A row the table
  itself refuses (class 23) still proves the privilege check passed; permission denied does not.
- After the services start, the first `os-retention` run finishes any line-less pending batch as
  `restored_without_detail`: every dump, including this restore's safety dump, leaves the data of
  `assaf_os.os_statement_batch_lines` out (`--exclude-table-data`).
- `mia restore` pauses the jobs itself, before it drops anything (the `jobs-paused` marker `mia jobs
  pause` makes), because `mia job` takes no state lock and a timer firing mid-restore would touch a
  half-restored or half-repaired database. They stay paused, also after an abort: check readiness, then
  `sudo mia jobs resume`. The 15-minute backup timer skips its slots while the restore holds the lock.
- Loopback authentication is not proven by steps 3 and 5 while `pg_hba.conf` still has the image's
  default `trust` lines for `127.0.0.1` and `::1` (V2_PG_CONTRACTS.md section 22, E21): a wrong password
  still connects, so these steps prove the role's posture and its privileges, not its password. Close
  those lines first: `ops/gcp/vm/pg-hba-harden.sh` (runbook step 1a, `docs/runbooks/APP_ROLE_DEMOTE.md`).
  After it, a wrong password is refused at the TCP line and steps 3 and 5 prove the password too.
- A function in schema `public` that has the default PUBLIC EXECUTE would stop every restore at step 2:
  `pg_restore --no-privileges` recreates it without its ACL, the V2-14a `--reapply` does not revoke it,
  and V2-15's exact validator rejects any PUBLIC privilege the ledger roles hold. Today `public` has no
  function (no migration creates one). A migration that adds one must also revoke PUBLIC EXECUTE.

Reason codes (stderr, a fixed set; nothing else is printed about the cause):

| Reason code | Meaning and what to do |
|---|---|
| `restore_catalog_unreadable` | a catalog query through `docker compose exec postgres` failed. Check the postgres container. |
| `app_role_reapply_unavailable` | `mia_app` exists but `/opt/mia/app-role-demote/app_role_demote.py` or `sql/app-role-demote.sql` is missing. Reinstall them from the approved source (V2-14a runbook). |
| `app_role_reapply_failed` | the helper failed. It prints its own `reason=app_role_...` line just above. |
| `ledger_reapply_unavailable` | the restored database has `assaf_os`, but `MIA_TAG` in `/opt/mia/tag.env` is unusable, the deployed `provision_ledger.py` is missing under `/opt/mia/src/<tag>/`, or `/etc/mia/os-ledger-migrate.env` is missing or holds another key. |
| `ledger_reapply_failed` | `provision_ledger.py` refused or could not run (it prints only `ledger_provision_refused` or `ledger_owner_go_sha_required`). Usual causes: the image has no 40-hex `MIA_BUILD_SHA`, `mia_app` is missing, `/etc/mia/runtime.env` does not hold a valid ledger DSN for an existing role, the ledger runtime role does not exist and no DSN is staged for it (provision it explicitly, then restore again), or V2-15's exact validator rejects the restored `assaf_os` (an object it does not know, a PUBLIC privilege). The transaction rolled back, so the schema is as the restore left it. |
| `main_dsn_invalid` | `/etc/mia/runtime.env` does not hold exactly one canonical `MIA_DATABASE_URL`. |
| `main_role_validation_failed` | the real validator rejected the main role (unsafe attribute, database owner, membership, or access to `assaf_os`), or the deployed image could not run it. Until the `trust` lines are closed (step 1a) a wrong DSN password does not fail here. |
| `owner_session_revoke_failed` | the owner-auth database could not be updated, or only some of its tables exist. |
| `app_role_access_probe_failed` | the main DSN role cannot SELECT or INSERT on an application table (permission denied, or no application table at all). The restore is not usable. While loopback is still `trust` this proves privileges, not the password. |

Fresh cluster (a new VM with no `mia_app`, or with `mia_app` but no ledger runtime role and no DSN
staged for it): a dump that contains `assaf_os` stops at `ledger_reapply_failed`. Step 2 pins the main
role and never writes the runtime DSN. Recreate the roles explicitly first (V2-14a `stage`, then V2-15
provision), then run `mia restore` again.

### On Assaf's PC: the drill uses `ledger_posture.py`

The drill has no `mia_app`, no deployed image and no `mia` script, so it proves the ledger part
with `ledger_posture.py`, a library the drill imports (no command line, never on the VM). It:

1. re-owns the schema and every table, view, sequence, function, procedure and enum/domain type
   in it to `assaf_os_owner`;
2. revokes everything from PUBLIC;
3. re-applies the reviewed grant set, a file containing only `GRANT`, `REVOKE` and
   `ALTER DEFAULT PRIVILEGES` statements that name `assaf_os`. The file is exactly the statements
   of V2-15's `ledger_grant_statements()` (`app/services/os_ledger/role_policy.py`), one per line,
   which are also the tail of the plan `provision_ledger.py --target mia-vm --mode reapply` prints.
   It is **not** the whole printed plan: that starts with `CREATE ROLE` and revokes from named roles,
   and is refused (`ledger_grants_invalid`). Anything else in the file is refused, and no quoting or
   `$` is allowed;
4. checks the result and fails unless: the schema and objects are owned by `assaf_os_owner`; nothing
   is granted to PUBLIC; no grantee other than `assaf_os_owner`, `assaf_os_ledger` and
   `assaf_os_retention` holds anything, columns included; and `assaf_os_ledger` holds no `DELETE` or
   `TRUNCATE`.

Steps 1 to 3 are one transaction: all or nothing. The drill does this on every restore that contains
the schema and **fails if the schema is there and no `--ledger-grants-file` is given**, because
the posture could not be proven. A dump without `assaf_os` passes with the schema recorded as
absent. The drill creates `assaf_os_owner`, `assaf_os_ledger` and `assaf_os_retention` as `NOLOGIN`
roles on the scratch server (a dump does not carry roles); production never creates them here. `ledger_posture.py` is a
floor under V2-15's exact validator, which the VM step above runs for real.

### The restore-drill test

`tests/ops/test_restore_reapply.py` runs the real restore arm of `mia`, the real
`app_role_demote.py`, V2-14's real migration and validator, and the real V2-15 `provision_ledger.py`
(first `--mode provision` to build the database, then `--mode reapply` through the restore arm)
against a disposable PostgreSQL 16 on `127.0.0.1:56218`. The only substitution is the script's pin
table, whose port and env-file path `tests/ops/restore_ledger_driver.py` swaps for the disposable
server; every statement, validator and refusal is the script's own. It dumps a database that has both
postures, restores it through the same flags, and asserts: V2-14's `_validate_main_role_catalog` and
V2-15's `validate_ledger_catalog` and `validate_ledger_role` pass, `mia_app` can SELECT and INSERT,
the runtime role keeps its password and reads (but cannot delete) the ledger data, `/etc/mia` is
never written, `revoked_before` is set, the safety dump has no batch-line data, the jobs are paused,
and a failing step leaves the services down and is retryable. It fails, rather than skips, when V2-15
is missing. The cases that need the server skip only when
`MIA_RESTORE_DRILL_TEST_POSTGRES_URL` is unset:

```bash
docker run --detach --pull=never --restart unless-stopped --name assaf-os-v2-10-restore-pg \
  --memory 512m --cpus 1 -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=restore_drill \
  -p 127.0.0.1:56218:5432 postgres:16-alpine
MIA_ENV=test MIA_RESTORE_DRILL_TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:56218/restore_drill \
  uv run --offline pytest -p no:warnings -rs tests/ops/test_restore_reapply.py tests/ops/test_pg_hba_harden.py
```

`tests/ops/test_pg_hba_harden.py` shares the opt-in: its behaviour proofs build a throwaway `initdb`
cluster on its own port inside that container and never touch anything else.

## A real recovery from these backups

1. Pause the timer so a half-restored database is not backed up: `sudo systemctl stop
   mia-backup-offsite.timer`. (A slot that fires during `mia restore` is skipped anyway, but the
   window between a failed restore and the next try is not covered by the lock.)
2. Download the newest pair: `gcloud storage cp gs://<bucket>/15min/mia-<stamp>.dump.enc
   gs://<bucket>/15min/mia-<stamp>.manifest.json .`. Check the manifest's `ciphertext_sha256`.
3. Decrypt with the offline key (iterations are in the manifest, today 600000):
   `openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 -md sha256 -pass file:mia-backup.key -in
   mia-<stamp>.dump.enc -out mia.dump`. Check `plaintext_sha256`, then `head -c5 mia.dump` is `PGDMP`.
4. Copy it to the VM, then `sudo mia restore mia.dump`. It pauses the jobs, saves the current
   database first, restores, re-applies the roles and grants (reason codes above; nothing else to
   run) and only then starts the services. Check `sudo mia status`, `sudo mia jobs resume`, then
   `sudo systemctl start mia-backup-offsite.timer`.

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
