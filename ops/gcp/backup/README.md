# Off-host database backup and restore drill (V2-10)

Status: **code only, not activated.** Nothing here runs on its own. Creating the bucket, the
service-account permissions, the key secret, installing the timer on the VM and the first drill
each need Assaf's explicit go. Evidence level today: `CODE_CHECKED` and `LOCAL_TESTED` (fakes, plus
a rehearsal against a real PostgreSQL 16 container). Not `STAGING_VERIFIED`, not `DEPLOYED`.

## What it does

After Assaf OS's first non-import receipt, follow
[`CUTOVER_RUNBOOK.md` section 6](../../assaf-os/CUTOVER_RUNBOOK.md#6-after-the-first-non-import-receipt)
for authority recovery around this database restore: containment with the loaded kill switch,
G22 fence, G21 database restore, another G22 fence of the restored authority, then separately
approved `restore-begin` and `restore-finish` at the original expected epoch. Retain the original
fence evidence outside the dump. The authority commands enforce RPO <=15 minutes; final service
recovery still needs measured RTO <=2 hours and live readback. This is an operator procedure,
never an automatic consequence of restoring the database.

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
- The run holds that lock **shared** from before the key fetch until the dump is on disk (not during the
  upload), so a restore cannot start between the check and the dump. The flip side, in one line: a
  `mia` command started during those seconds reports
  `another Mia operation (or a backup dump) is running; try again in a minute`;
  `pg-hba-harden.sh --apply|--rollback` reports `reason=hba_operation_busy` and `app-role-demote.sh`
  `reason=app_role_operation_busy`. Nothing was changed: retry in a minute, do not roll back.
- These three behaviours (the shared lock, the marker skip, the SIGTERM cleanup) are in the copy the
  timer runs, `/opt/mia/backup/backup_offsite.py`, only after `install_on_vm.py` is run again from the
  deployed tag (step 4 of the setup above). A deploy alone does not update it.
- A slot is also **skipped** (`skipped code=restore_incomplete`, exit 0, no failure recorded) while
  `/etc/mia/restore-incomplete` exists. `mia restore` writes it before it drops the database and removes
  it only after every post-restore step succeeded, so a restore that aborted (`ledger_reapply_failed`
  and the other reason codes below) is never dumped and uploaded as the newest good backup. Until the
  next successful `mia restore` the backups are skipped and `--status` goes stale: that is intended.
- If the 25 minute `TimeoutStartSec` stops a run, systemd's SIGTERM is turned into a normal exit by the
  entry point, so the run directory (the plaintext dump and the key file) is removed on the way out.
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
Record the report in `HANDOFF.md` under the SHA it was run at. It names a failed tool and its exit
code and never the tool's own error text (a restore error can print the key or row that failed), so
it holds no row data; a run that refuses prints the tool's error on stderr only, which is not part of
the report. RTO caveat: the drill times a
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
safety dump first).

**The services stay down until a restore succeeds (V2-10b).** `mia restore` writes
`/etc/mia/restore-incomplete` (root, 0600) after its safety dump and just before `DROP DATABASE`,
and removes it only after every step below succeeded, right before it starts the services. While the
marker exists `start_services` refuses with `mia: restore_incomplete: run mia restore again`, and
that is what `mia deploy`, `mia settings`, `mia https enable` and `mia go-live` all end in. **Operator
note:** the deploy that fixes the cause of a `ledger_reapply_failed` (say, an image with a proper
`MIA_BUILD_SHA`) will build, fetch settings and then stop at that refusal, by design: run
`sudo mia restore <same dump>` again, and the services start at the end of it. A restore that stops
before `DROP DATABASE` (a missing dump, a failed safety dump) changed nothing and leaves no marker.
The off-host backup also skips its slots while the marker exists (see "Watching it"). The marker guards
`start_services` only: a raw `docker compose ... up` (as in `docs/runbooks/APP_ROLE_DEMOTE.md`), a
`mia job` after `jobs resume`, `mia os-job` and `mia os-authority` do not check it, so run none of them
while it exists. The steps, in
this order:

Before `DROP DATABASE`, `mia restore` also atomically records `restore-attempt.json` in
`/var/lib/mia/os-authority` (UID 10001, mode 0600), outside the dump. Its
`pre_restore_fence_record_present` boolean records whether `ledger-fence.json` existed before
replacement. A later fence without matching incident evidence is marked
`fence_record_minted_after_restore=true`; the standard authority restore commands then STOP.
For a fresh cluster or lost evidence directory, follow the owner's explicit exception procedure
in `ops/assaf-os/CUTOVER_RUNBOOK.md` section 6. RTO/RPO booleans are meaningless for timestamps
first minted after restoration. A successful authority restore-finish archives the attempt so
it cannot mark a later incident. Failure to write this attempt leaves no new incomplete marker,
the database untouched, and the services remain stopped (`restore_evidence_invalid`). Repair
the evidence directory and rerun `sudo mia restore <same dump>`. To abandon this pre-DROP restore,
use `sudo mia settings` to call `start_services` again, retaining any incident containment;
an existing incomplete marker from an earlier restore still blocks startup.

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
  anything, its transaction rolls back and the restore stops with `ledger_reapply_failed`. A new VM
  must have its roles made **before** the restore, not after a failed one: see "Fresh cluster" below.
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
  `sudo mia jobs resume`. The 15-minute backup timer skips its slots while the restore holds the lock,
  and after an abort for as long as the `restore-incomplete` marker exists.
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
| `restore_evidence_invalid` | the durable pre-restore evidence could not be written: no new incomplete marker, database untouched, services remain stopped. Repair the evidence directory and rerun the restore, or abandon this pre-DROP restore and use `sudo mia settings` (`start_services`) to start services again while retaining incident containment. An earlier incomplete marker still blocks startup. |
| `app_role_reapply_unavailable` | `mia_app` exists but `/opt/mia/app-role-demote/app_role_demote.py` or `sql/app-role-demote.sql` is missing. Reinstall them from the approved source (V2-14a runbook). |
| `app_role_reapply_failed` | the helper failed. It prints its own `reason=app_role_...` line just above. |
| `ledger_reapply_unavailable` | the restored database has `assaf_os`, but `MIA_TAG` in `/opt/mia/tag.env` is unusable, the deployed `provision_ledger.py` is missing under `/opt/mia/src/<tag>/`, or `/etc/mia/os-ledger-migrate.env` is missing or holds another key. |
| `ledger_reapply_failed` | `provision_ledger.py` refused or could not run (it prints only `ledger_provision_refused` or `ledger_owner_go_sha_required`). Usual causes: the image has no 40-hex `MIA_BUILD_SHA`, `mia_app` is missing, `/etc/mia/runtime.env` does not hold a valid ledger DSN for an existing role, the ledger runtime role does not exist and no DSN is staged for it (a new VM: "Fresh cluster" below, and do not just restore again), or V2-15's exact validator rejects the restored `assaf_os` (an object it does not know, a PUBLIC privilege), or **the dump is older than the ledger schema the running image expects** (see "A dump older than the current ledger schema" below: repeating the restore with this image can never pass). The transaction rolled back, so the schema is as the restore left it. The services stay down until a restore succeeds (the `restore-incomplete` marker). |
| `main_dsn_invalid` | `/etc/mia/runtime.env` does not hold exactly one canonical `MIA_DATABASE_URL`. |
| `main_role_validation_failed` | the real validator rejected the main role (unsafe attribute, database owner, membership, or access to `assaf_os`), or the deployed image could not run it. Until the `trust` lines are closed (step 1a) a wrong DSN password does not fail here. |
| `owner_session_revoke_failed` | the owner-auth database could not be updated, or only some of its tables exist. |
| `app_role_access_probe_failed` | the main DSN role cannot SELECT or INSERT on an application table (permission denied, or no application table at all). The restore is not usable. While loopback is still `trust` this proves privileges, not the password. |

#### A dump older than the current ledger schema (`ledger_reapply_failed` that repeats)

**Cause.** Once a later ledger migration ships (a table, column or grant the dump does not have), a
dump taken before it, restored with the current image, always fails step 2: the reapply issues grants
on objects the dump does not hold, and V2-15's exact validator expects the newer catalog. Fixing the
usual causes and running `mia restore` again then loops, and `mia os-ledger migrate` refuses on the
unrepaired posture, so the way forward is not to retry with the same image.

**The way out.** Restore with the image whose ledger schema matches the dump, then move forward:

1. `sudo mia deploy <the tag whose ledger schema matches the dump>`. The marker makes `start_services`
   refuse (by design, the database is half restored), so expect that refusal at the end of the deploy:
   the old image is built and `tag.env` points at it, which is all the next step needs. `mia deploy`
   needs `/opt/mia/src/<tag>` and `mia` keeps only the three newest sources, so an older tag has to be
   put back first (a normal approved upload of that commit). The deploy runs that tag's own `mia`: a
   tag older than V2-10b knows no marker and starts the services on the half-restored database, so
   with such a tag run step 2 at once, and remove a stale `/etc/mia/restore-incomplete` by hand only
   after step 2 succeeded.
2. `sudo mia restore <dump>`. The reapply now matches the schema; the services start at the end.
3. Roll forward to the current image: `sudo mia deploy <current tag>`.
4. `sudo mia os-ledger migrate --approved-sha <sha>` applies the ledger migrations the dump predates.
5. `sudo mia os-provision runtime --approved-sha <sha>` re-provisions the runtime role's grants and
   DSN for the new schema.

**Record it.** The cutover or ledger runbook should record which image tag matches which dump (the
dump's `created_at_utc` against the deploy that was running then), so a restore under pressure does
not need this deduction. Nothing is exposed today: V2-14 and V2-14b migrate together and production
has no `assaf_os` yet.

#### Fresh cluster: a new VM, after the old one is lost

A new VM has the superuser `mia` and the database `mia` the deploy made. It has no `mia_app`, no
`assaf_os_*` role and no staged ledger DSN, because roles live in the cluster and not in a dump.
**Do not run `mia restore` first** when the dump holds `assaf_os`:

- Step 1 is skipped (`mia_app` does not exist) and step 2 stops with `ledger_reapply_failed`: it pins
  the demoted main role and never writes the runtime DSN, so it refuses before it creates a role.
- There is then no way forward on that database. V2-14a's `stage` refuses on the restored database
  with `app_role_ledger_access_unsafe` (`pg_restore --no-privileges` left PUBLIC EXECUTE on the
  `assaf_os` functions, and only the V2-15 reapply that needs `mia_app` clears it), and V2-15's
  `--mode provision` refuses because there is no `mia_app`.

Make the roles first, on the new VM's own database, in the order production used, and restore last:

1. V2-14a: the demote, `docs/runbooks/APP_ROLE_DEMOTE.md` steps 2 to 6. The stage creates `mia_app`
   and the activation moves the main DSN to it.
2. V2-14: `sudo mia os-ledger migrate --approved-sha <sha>` creates `assaf_os` and `assaf_os_owner`.
3. V2-15: `sudo mia os-provision runtime --approved-sha <sha>` (`ops/assaf-os/LEDGER_ROLE.md`)
   creates `assaf_os_ledger` and `assaf_os_retention`, and stages this VM's own ledger DSN in
   `runtime.env`.
4. E8: `sudo mia os-provision retention --approved-sha <sha>` gives `assaf_os_retention` its password
   and writes the job-only `/etc/mia/os-retention.env`.
5. `sudo mia restore <dump>`. Steps 1 and 2 now find their roles and the staged DSN. Step 4 is
   skipped on a new VM (`owner_auth_database_absent`): the owner-auth database is not part of this
   restore, and `provision_auth.py` makes it.

If `mia restore` already ran first, nothing cluster-wide was created (the ledger script refused before
`CREATE ROLE`), so only the database needs resetting. On a new VM it holds nothing but the failed
restore, and `/var/lib/mia/backups` keeps that restore's safety dump:

```bash
sudo docker compose -p mia -f /opt/mia/compose.yaml --env-file /opt/mia/tag.env exec -T postgres \
  psql -U mia -d postgres -v ON_ERROR_STOP=1 \
  -c 'DROP DATABASE IF EXISTS mia WITH (FORCE)' -c 'CREATE DATABASE mia OWNER mia'
```

Then make the application tables the way the first deploy did (`sudo docker compose -p mia -f
/opt/mia/compose.yaml --env-file /opt/mia/tag.env run --rm --no-deps -T migrate`) and run steps 1 to
5. The failed restore left the `restore-incomplete` marker: `start_services` refuses until that last
`mia restore` succeeds, which is what removes it. Step 1's raw `docker compose ... up` does not check
the marker and starts the app on the reset, freshly migrated database; that is expected in this order
(it is empty, not half restored), but run no `mia` job against it before the last restore. Never reset a database that holds data you need.
The drill proves the order, the failure of the other order and this recovery against the real
scripts: `test_drill_a_new_vm_*` in `tests/ops/test_restore_reapply.py`.

**The new-VM provision command (V2-31).** Steps 3 and 4 are `mia os-provision runtime|retention
--approved-sha <sha>`; there is no gap left. What the command does, so the order above can be
trusted: it takes `/var/lock/mia-state.lock` (a backup dump in progress makes it report busy: retry),
refuses unless `/opt/mia/tag.env` exists, `/etc/mia/os-ledger-migrate.env` holds exactly its one key
and `--approved-sha` is a 40-hex SHA, then runs `provision_ledger.py --target mia-vm --mode provision`
(`runtime`) or `provision_retention.py --target mia-vm` (`retention`) from the **deployed tag's**
`/opt/mia/src/<MIA_TAG>/ops/assaf-os/` (both files must be there), mounted read-only into the
`os-ledger-migrate` image and run as root with `/etc/mia` mounted **writable**, because each script
stages its DSN in a VM-owned file there before it commits. The approved SHA must equal the image's
`MIA_BUILD_SHA`. A lost VM is therefore rebuilt as: deploy the integrated tag, steps 1 to 5 above.

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
never written, `revoked_before` is set, the jobs are paused, and a failing step leaves the services
down and is retryable. With a real pending statement batch (V2-14b's `os_statement_batch_lines`) in
the live database, neither the safety dump nor the off-site dump carries a row of it, a control dump
without the exclusion does, and the restore of the off-site dump still passes both validators. It
also proves the new-VM cases under "Fresh cluster": the documented order restores, the other order
does not, and the recovery from it works. It fails, rather than skips, when V2-15 is missing. The
cases that need the server skip only when
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
   mia-backup-offsite.timer`. (This is belt and braces: a slot that fires during `mia restore` is
   skipped by the lock, and after a failed restore by the `restore-incomplete` marker, until a restore
   succeeds.)
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
