# V2-14a: owner-gated main application role cutover

Status: implementation and disposable local evidence only. No production commands in
this runbook have been executed by the builder. Independent HEAVY review and Assaf's
approval are required before production. Issue #151, epic #153; the V2-14 ledger schema
(PR #157) is merged but its migration is a separate owner gate and must not be run by this
procedure. The `--reapply` post-restore repair (below) is the V2-14a finishing work.

## Migration decision and authority boundary

Keep the existing migration DSN. `ops/gcp/vm/mia` starts Compose's `migrate` service;
`ops/gcp/vm/compose.yaml` loads `/etc/mia/settings.env`, then `/etc/mia/runtime.env`
for both `migrate` and `app`. `mia-migrate` resolves to `app.workers.migrate:main`,
which calls `init_db()` (metadata create_all) and `apply_migrations(get_engine())`.
Both use `Settings.database_url`, hence **today both connect as superuser mia**.
Bootstrap sets `POSTGRES_USER=mia` and creates the original runtime DSN once.

After the cutover, the same paths connect as `mia_app`. Ownership of all existing
public tables (including partitioned/foreign tables), sequences, views, materialized
views and routines is transferred. Indexes and table-owned sequences follow table
ownership. `mia_app` gets USAGE and CREATE on `public`, CONNECT on the database,
and no database ownership, role membership, or database CREATE. Current SQL
migrations need ordinary owner DDL only. Both the original superuser path and the
new app path remain usable; no Compose or deploy script change is necessary.

This is the smallest safe change, with an explicit tradeoff: a compromised runtime
can alter/destroy **public** application data because it owns that schema's objects.
Separating runtime DML from migration DDL is a future, larger change. New public
objects must be created by `mia_app`; ownership cannot be supplied by default ACLs.
Future privileged migrations require separate review. If maintenance as `mia`
creates new public objects, re-run provisioning before returning to `mia_app`.

Default ACLs for `mia_app` retain owner rights and remove PUBLIC table/sequence
rights in public and PUBLIC function EXECUTE globally for future functions. Existing
object ACLs and dedicated Owner OS role grants are preserved. No ACL, ownership,
or object in `assaf_os` is changed. Schema, table, column, sequence and routine
privileges there (including PUBLIC privileges without schema USAGE) cause refusal,
matching V2-14's catalog checks. Unsafe existing roles/memberships or public
extension-owned relations/functions also cause a transaction rollback and require
separate review. No blanket `REASSIGN OWNED` is used.

Revoking database CREATE from PUBLIC affects every role relying on that grant;
Assaf must approve it after confirming no other user of this dedicated application
database needs to create schemas/extensions. Current migrations only create public
objects. CONNECT and TEMP are preserved. The `public` schema owner is unchanged.
`mia` remains superuser for maintenance, backups and restores; its password is not
changed. V2-14 uses its own privileged migration DSN, separately reviewed.

## Approvals and prerequisites

Assaf approves the reviewed revision, maintenance window, backup/rollback custody,
public-object ownership transfer, PUBLIC database CREATE revocation, app DSN swap,
explicit migration smoke, restarts and final enforce activation. This approval
does not authorize ledger migration/activation, role cleanup, or unrelated writes.

Before starting, deploy the independently reviewed CI-green revision using the
normal release procedure, **in observe mode with the old DSN**. Confirm readiness.
The builder's local tests are not a production deployment or acceptance claim.
Keep all three helper files together outside the rotating source-retention tree:

```bash
# Owner-operated on the VM, using the exact approved source checkout.
sudo install -d -m 700 /opt/mia/app-role-demote/sql
sudo install -m 700 /opt/mia/src/<approved-tag>/ops/gcp/vm/app-role-demote.sh /opt/mia/app-role-demote/
sudo install -m 600 /opt/mia/src/<approved-tag>/ops/gcp/vm/app_role_demote.py /opt/mia/app-role-demote/
sudo install -m 600 /opt/mia/src/<approved-tag>/ops/gcp/vm/sql/app-role-demote.sql /opt/mia/app-role-demote/sql/
```

Run the wrapper, not the Python helper directly: it takes the same
`/var/lock/mia-state.lock` as deploy/settings/restore. Helpers do not start containers,
restart services, fetch settings, or update Secret Manager. `/etc/mia` must remain
root-owned mode 700; runtime and staged/rollback files must be root-owned mode 600.
Never echo, cat, source, paste, upload, or enable tracing for credential files.
The helper generates a 64-character random hex password locally, sends credentials
over subprocess stdin, and suppresses provider output. Standard PostgreSQL statement
and error logging is disabled transaction-locally during credential provisioning.
If additional SQL audit extensions/exporters record credential statements, review
their redaction before provisioning; they are outside this helper's logging control.

## Ordered cutover

1. **Backup first**: `sudo mia backup`. Record the returned dump path privately.
   Validate the archive with `pg_restore --list` through the existing postgres
   container, discarding the listing. Record the current image/tag and job pause state.
   Backups contain sensitive data and remain under `/var/lib/mia/backups`.
1a. **Close loopback `trust` in PostgreSQL** (E21, `V2_PG_CONTRACTS.md` section 22; FORK-PROD-ORDER
   step 1a: after the deploy of the integrated SHA, before the demote). Until it is done, the demote
   below gives no protection against code running on the VM: `pg_hba.conf` lets any process connect
   to `127.0.0.1:5432` as any role, `mia` included, with any password. See "Step 1a in detail".
   Pause the jobs (`sudo mia jobs pause`, preserving an already-paused state), keep the verified
   backup from step 1 and Assaf's go, then:

   ```bash
   H=/opt/mia/src/<approved-tag>/ops/gcp/vm/pg-hba-harden.sh
   sudo bash "$H" --check      # exit 1 until applied: expect hardened=false, apply_ready=true,
                               # dsn_unverified=none (anything else: fix that DSN first, see below);
                               # dsn_keys_checked= lists every DSN key it will log in with afterwards
   sudo bash "$H" --apply      # expect applied=true hardened=true
   curl --fail --silent --show-error http://127.0.0.1:8000/health/ready
   sudo bash "$H" --check      # expect hardened=true (exit 0)
   ```

   Any failure, or a non-200 readiness: `sudo bash "$H" --rollback`, check readiness again, and stop.
2. `sudo /opt/mia/app-role-demote/app-role-demote.sh --check`. This only reads the
   catalog/runtime file and prints booleans. Before provisioning, `role_exists=false`
   and `active_is_app=false` are expected. A successful command exit means the check
   ran, not that every posture boolean is safe. Pause new jobs with `sudo mia jobs
   pause` and wait for existing `mia-job@mia-due-scan` and
   `mia-job@mia-ingest-knowledge` units to finish. Preserve an already-paused state.
3. `sudo /opt/mia/app-role-demote/app-role-demote.sh`. This transactionally provisions
   ownership/ACLs and stages `/etc/mia/app-role.staged.env`. The original superuser
   DSN is saved once to `/etc/mia/app-role.rollback.env`. Keep that file: besides this
   runbook's rollback, `ops/assaf-os/CUTOVER_RUNBOOK.md` step 3 derives
   `/etc/mia/os-ledger-migrate.env` from it (the same `mia` DSN under the key
   `MIA_OS_LEDGER_MIGRATION_DATABASE_URL`, root-only, mode 600). The active runtime file is
   unchanged. A rerun keeps the password and logical catalog state; unmanaged existing
   `mia_app` without a staged file is refused. A failed SQL transaction retains the
   staged credential for retry, and never switches the app. The helper tests a TCP
   login as `mia_app` after provisioning.
4. Repeat `--check`. Require `role_exists`, `role_safe`, `no_membership`,
   `not_database_owner`, `public_owned`, `public_create` true and
   `database_create`, `ledger_access` false. `active_is_app` remains false.
5. `sudo /opt/mia/app-role-demote/app-role-demote.sh --activate`. It rechecks the
   catalog and staged TCP credential, swaps only the DSN/posture keys atomically in
   `/etc/mia/runtime.env`, and sets observe. Other runtime entries are preserved.
   Run the actual migration entry point and recreate the app with the existing image:

   ```bash
   sudo docker compose -p mia -f /opt/mia/compose.yaml --env-file /opt/mia/tag.env run --rm --no-deps -T migrate
   sudo docker compose -p mia -f /opt/mia/compose.yaml --env-file /opt/mia/tag.env up -d --no-deps --force-recreate app
   curl --fail --silent --show-error http://127.0.0.1:8000/health/ready
   ```

   Stop and roll back on migration failure or non-200 readiness. Verify
   `reason=app_role_ok safe=True ready=True` in the new app's readiness log and the
   expected SHA at `/health`. A 200 in observe mode alone does not prove safe posture.
6. `sudo /opt/mia/app-role-demote/app-role-demote.sh --enforce`, then repeat the app
   recreate command and readiness request. The mode lives in VM-owned runtime.env,
   which overrides settings.env, so a subsequent settings fetch cannot silently
   undo it. Repeat `--check`: `active_is_app=true`, `enforce=true`, safe catalog.
   Confirm existing public application functions with owner-approved smoke tests.
7. Restore jobs only if they were running before this window: `sudo mia jobs resume`.
   Record SHA, backup path, boolean checks, readiness status and owner acceptance.
   Keep the protected rollback credential file available. Do not run ledger migration
   until the separate V2-14 owner gate.

## Step 1a in detail: `pg-hba-harden.sh`

The official postgres image runs `initdb` with no `--auth` flags, so `pg_hba.conf` starts with
`host all all 127.0.0.1/32 trust` and `::1/128 trust`, above the scram line the image appends.
Postgres and the app share the host network, so every process on the VM could open the database as the
superuser. Confirm first, read-only: `sudo docker exec mia-postgres-1 sh -c 'grep -v "^#" $PGDATA/pg_hba.conf'`
(or just run `--check`).

What `--apply` does, in this order, and nothing else (it never deploys, restarts or fetches settings):

1. Refuses unless every role a live DSN uses has a SCRAM verifier. The fixed list is `mia`, `mia_app` (when
   present), `assaf_os_session`, `assaf_os_ledger` and `assaf_os_retention`; the users of every
   `*DATABASE_URL` key in the DSN files (`runtime.env`, `settings.env`, `os-ledger-migrate.env`,
   `os-retention.env`; V2-10b, E28) are added, including `MIA_OWNER_OS_READ_DATABASE_URL` and
   `MIA_OWNER_OS_ACTION_DATABASE_URL`. Run `--check` well before the window: a surprise key there blocks
   `--apply` until it is fixed. A role that does not
   exist yet, or cannot log in (the NOLOGIN `assaf_os_owner`), is not a DSN role. `mia` must exist.
   A live DSN with no password is refused too (`hba_dsn_without_password`), and so is one in a form the
   script cannot parse (`hba_dsn_unverified`: another host, `localhost`, a query string), because its login
   could not be tried after the reload. Rewrite it as `postgresql://<role>:<password>@127.0.0.1:<port>/<db>`
   first. Nothing is changed on a refusal.

   Since V2-14b, V2-15's provision creates `assaf_os_retention` as LOGIN with **no password** (E8). From
   then on it is a DSN role without a verifier and blocks `--apply` (`hba_role_without_scram`) until V2-31
   gives it one. Run step 1a before the V2-15 provision, as FORK-PROD-ORDER does (step 1a, then the demote,
   then the ledger at step 4).
2. Copies `pg_hba.conf` to `pg_hba.conf.pre-e21-<UTC time>` in the same directory and compares the copy.
3. Rewrites every `host*` line whose method is `trust` (127.0.0.1, `::1`, and `replication`) to
   `scram-sha-256`, keeping columns and comments. The new file must parse with no TCP `trust` rule left, or
   the backup is put back before any reload.
4. Reloads, then proves it took effect: a wrong password must be refused over TCP. Then every readable live
   DSN must log in with its own password. If either fails, the backup is restored and reloaded and the
   script exits 1, so a bad password never leaves the app locked out.

`--rollback` restores the newest backup and reloads. Both are idempotent: `--apply` on a hardened cluster
makes no backup, and `--rollback` when the file already equals the backup says so. `--check` exits 0 only
when no TCP `trust` rule is left. It prints booleans and role names, never a DSN, a password or the file.

**The unix socket stays `trust`, on purpose.** The socket exists only inside the postgres container, and
`mia backup`, `mia restore`, `app_role_demote.py`, `backup_offsite.py`, `provision_auth.py` and the restore
drill all reach PostgreSQL as `mia` through `docker compose exec` over it, with no password. Switching it to
a password would lock out the demote itself and the restore a new VM exists to run. That is why new clusters
get only `POSTGRES_INITDB_ARGS=--auth-host=scram-sha-256` (`compose.yaml`, `bootstrap.sh`), not
`--auth-local`. It is a deliberate narrowing of E21's literal text.

After it, `mia restore`'s main-role validator and access probe really do prove the password: a wrong one is
refused at the TCP line.

**The deploy that brings `compose.yaml` restarts PostgreSQL once.** The `postgres` service gained
`environment: POSTGRES_INITDB_ARGS`, which changes its configuration, so the next `mia deploy`
(`compose up -d`) recreates that container: a brief database restart, with the data volume and the cluster's
`pg_hba.conf` untouched (initdb arguments only act on a new data directory). Expect `/health/ready` to fail
for a few seconds during that deploy, and do that deploy before step 1a, as FORK-PROD-ORDER already orders it.

| Reason code | Meaning and what to do |
|---|---|
| `hba_root_required` | not run as root (exit 1). |
| `hba_operation_busy` | another `mia` operation, or an off-host backup dump (V2-10b), holds `/var/lock/mia-state.lock` (exit 1). Nothing was changed: retry in a minute; this is not a reason to roll back. |
| `hba_database_unreachable` | `docker compose exec postgres psql` failed (exit 2). Is the postgres container up? |
| `hba_file_unusable`, `hba_rules_unreadable`, `hba_roles_unreadable` | the server did not answer the catalog query as expected (exit 2). Nothing was changed. |
| `hba_role_without_scram` | a login role a DSN uses has no SCRAM verifier, or `mia` is missing (exit 2). The role names are printed on `roles_without_scram=`. Set the password again (`ALTER ROLE ... PASSWORD`) under `password_encryption = 'scram-sha-256'` and update the DSN, then retry. |
| `hba_dsn_without_password` | a live DSN names a role with no password (exit 2); it would be locked out. |
| `hba_dsn_unverified` | a live DSN is in a form the script cannot parse, so its login could not be checked after the change (exit 2). `dsn_unverified=` names the setting. Rewrite it in the canonical loopback form, then retry. A key named twice in one file is listed too (Compose reads the last line), and `<file>:noncanonical_key` means a DSN key in that file is lowercase, `export`-prefixed or has a space before `=`: keep exactly one `KEY=value` line per key. `--check` shows `apply_ready=false` for it. |
| `hba_rules_invalid_before_change` | the current `pg_hba.conf` already has a syntax error (exit 2). Fix it first. |
| `hba_rewrite_failed` | the copy or rewrite failed (exit 1). The file is restored if it had been touched. |
| `hba_rewrite_invalid_restored` | the rewritten file still had a TCP `trust` rule (for example in an `include`d file) or did not parse (exit 1). The backup was put back. |
| `hba_reload_failed_restored`, `hba_reload_not_effective_restored` | the reload failed, or a wrong password was still accepted after it (exit 1). The backup was put back. |
| `hba_login_failed_rolled_back` | a live DSN's password was refused after the change (exit 1). `key=` names the setting. The backup was put back and reloaded. Fix that DSN or role, then retry. |
| `hba_no_backup`, `hba_backup_unusable` | `--rollback` found no usable `pg_hba.conf.pre-e21-*` file (exit 1). |
| `hba_rules_invalid_after_rollback` | the restored file does not parse (exit 1). Inspect it by hand. |
| `hba_already_hardened`, `hba_already_restored` | not errors (exit 0): `--apply` found no TCP `trust` rule, or `--rollback` found the file already equal to the backup. Nothing was changed. |

## Exact rollback

Use the same reviewed helper even after enforce was enabled:

```bash
sudo mia jobs pause
sudo /opt/mia/app-role-demote/app-role-demote.sh --rollback
sudo docker compose -p mia -f /opt/mia/compose.yaml --env-file /opt/mia/tag.env up -d --no-deps --force-recreate app
curl --fail --silent --show-error http://127.0.0.1:8000/health/ready
sudo /opt/mia/app-role-demote/app-role-demote.sh --check
```

Rollback restores the saved `mia` DSN and forces observe; other runtime keys are
preserved. It neither deletes `mia_app` nor reverses ownership/ACLs. Superuser `mia`
can still operate every public object. Readiness may report
`app_role_demote_required safe=False ready=True`; that is expected during rollback.
Require `active_is_app=false`, `enforce=false`, and healthy application behavior.
Resume jobs only to their recorded prior state. A repeated rollback is safe.

If an old image must also be restored, use the normal exact-SHA rollback procedure
after the DSN rollback. **Do not use `mia restore` for this cutover rollback.** Its
`--no-owner --no-privileges` behavior recreates objects as `mia` and strips ACLs, so the
database needs the repair below before `mia_app` can use it. Rolling back the DSN never
needs a restore.

## Re-applying after a restore (`--reapply`)

`mia restore` runs `DROP DATABASE`/`CREATE DATABASE mia` and then
`pg_restore --no-owner --no-privileges` as superuser `mia`. Roles are cluster-global, so
`mia_app` survives. Everything database-local does not: every object is owned by `mia`,
every grant and default ACL is gone, and `mia_app` has no CREATE on `public`. Until
repaired, `mia_app` gets `permission denied` on every table and `mia-migrate` cannot ALTER.

`app-role-demote.sh --reapply` is that repair. It takes the same state lock and runs the
same reviewed SQL as the first demote, with these differences from `stage`:

- It re-runs the ownership transfer of every `public` table, sequence, view, materialized
  view, foreign table and routine to `mia_app`, the CONNECT, USAGE and CREATE grants, the
  PUBLIC database CREATE revoke, and the global PUBLIC EXECUTE revoke for future functions.
- It is idempotent: a second run changes no catalog row.
- It reads and writes no env file (`runtime.env`, staged and rollback files stay untouched),
  creates no role, rotates no password, restarts nothing, and prints booleans only.
- Before the demote exists (`mia_app` absent) it is a no-op,
  `{"reapplied": false, "role_exists": false}`, so a restore before step 2 of the production
  order still works. In reapply mode the SQL itself refuses to create the role.
- Every other guard still applies: an unsafe role, database ownership or membership aborts
  with the same reason code. An extension-owned object in `public` stops the run with
  `app_role_extension_object_requires_review`; the transaction rolls back and nothing
  changes. Review it before restoring service.
- It does not refuse residual `assaf_os` access. A restore resurrects PUBLIC EXECUTE on the
  ledger functions, because their `REVOKE` is a privilege that `--no-privileges` drops.
  `--reapply` never touches `assaf_os`; it reports `"ledger_access": true` and the next
  post-restore step, `provision_ledger.py --mode reapply` (V2-15), clears it.
- After the transaction it re-reads the catalog and fails with `app_role_check_failed` if
  `public` is not fully owned by `mia_app` or any posture boolean is wrong.

Output on success: `{"ledger_access": false, "reapplied": true, "role_exists": true}`.

**Who runs it.** After V2-10's post-restore step lands in `ops/gcp/vm/mia`, `mia restore`
runs it automatically: after `pg_restore`, before `start_services`, under the lock it already
holds, followed by `provision_ledger.py --mode reapply`. Because `mia restore` already holds
`/var/lock/mia-state.lock`, it must run `app_role_demote.py --reapply` directly: the wrapper
takes the same lock on a new file description and would report busy. Until V2-10 lands, an
operator restoring a post-demote dump runs
`sudo /opt/mia/app-role-demote/app-role-demote.sh --reapply` immediately after `mia restore`,
then recreates `app`. Never restore the services to traffic before it succeeds.

**Known limit of the posture check.** V2-14's `_validate_main_role_catalog`, and so
`/health/ready` under `enforce`, checks role attributes, database ownership, memberships and
access to `assaf_os`. It does not check access to `public` tables. After a restore that is
not repaired it still passes while every query is denied. V2-10's post-step must therefore
also probe real table access as `mia_app` (SELECT and INSERT on an application table), not
rely on the validator alone. The restore drill in
`tests/unit/test_app_role_demote_postgres.py` asserts the denial before `--reapply` and the
validator plus real SELECT and INSERT after it.

Not covered: `--reapply` transfers tables, sequences, views, materialized views, foreign
tables and routines. Custom types and domains in `public` are not transferred (the current
schema has none); a future migration that adds one needs the SQL extended.

## Readiness and evidence

| Main DSN posture | observe (default) | enforce |
| --- | --- | --- |
| Any rejected attribute / superuser | logs `app_role_demote_required`, permits readiness | 503 |
| Demoted, no private access | logs `app_role_ok`, permits readiness | 200 |
| Any assaf_os schema/object/column right | logs `app_role_ledger_access_unsafe`, permits readiness | 503 |
| Database owner / member / impersonated session | corresponding unsafe reason, permits readiness | 503 |
| Probe error or non-PostgreSQL | fixed reason, permits readiness | 503 |

All 200 entries assume the pre-existing database/schema readiness check passes.
Observe never adds a startup blocker. Enforce affects `/health/ready`, not liveness
or process startup. Error details, SQL, role identifiers, DSNs and passwords are not
included in posture logs or health responses.

### Evidence

As built (before the finishing work): baseline 3672 passed, 213 skipped; tests-first red
22 failed plus 5 errors; full default suite 3699 passed, 228 skipped; full suite with the
Owner OS database on the disposable server 3737 passed, 190 skipped; focused 57 passed with
zero skipped. An additional fresh HEAVY source review of that build found a POSIX test-fixture
ownership issue, which was corrected. Those counts are superseded by the run below.

Finishing work, rebased on master `bfb78d6` (which carries the V2-14 ledger schema). Logs are
local and uncommitted:

| Gate | Result | Log |
| --- | --- | --- |
| Tests-first red (reapply, restore drill, settings, real validator) | 60 failed, 51 passed | `.cache/v2-14a-finish-red.log` |
| Full default suite (`MIA_ENV=test`, PG URL unset) | 4753 passed, 291 skipped | `.cache/v2-14a-finish-full.log` |
| Focused role tests on disposable **56216** | 119 passed, 0 skipped | `.cache/v2-14a-finish-focused.log` |
| Ruff (`app tests ops`) | passed | |
| Widget behavioral checks | passed | `.cache/v2-14a-finish-widget.log` |

The focused command, run with the opt-in URL set so no case skips:

```bash
MIA_ENV=test MIA_APP_ROLE_TEST_POSTGRES_URL=postgresql+psycopg://postgres@127.0.0.1:56216/mia_role_test \
  uv run --offline pytest -p no:warnings -rs tests/unit/test_app_role_demote_postgres.py \
  tests/unit/test_app_role_demote_script.py tests/unit/test_app_role_posture.py
```

The default run skips the 27 PostgreSQL cases of `test_app_role_demote_postgres.py` because
the opt-in URL is unset, on top of the other opt-in skips. The fixture allows only the supplied
disposable admin URL at `127.0.0.1:56216/mia_role_test`; it creates unique databases and roles
and removes them. The restore drill runs `pg_dump` and `pg_restore` with the VM's exact
restore flags: from `PATH` when present, otherwise inside the disposable container with
`docker exec` (`MIA_APP_ROLE_TEST_PG_CONTAINER`, default `assaf-os-v2-14a-pg`).

The suite calls V2-14's real `_validate_main_role_catalog` (never a copy), the deployment
migration worker with `MIA_DATABASE_URL` changed to the demoted login, pending ALTER/CREATE
migrations, real catalog staging/rerun/cutover/rollback, the dump, restore and `--reapply`
drill, and sanitized failure and readiness tests. Loopback trust authentication proves SQL
authority and orchestration, not production password-auth configuration; the VM helper
separately verifies TCP login on cutover.

The full suite's existing `tests/conftest.py` overwrites MIA_DATABASE_URL with SQLite.
The dedicated test explicitly sets it after conftest and resets the engine, so its
real migration proof is not masked by that fixture. General SQLite suite results are
not claimed as a full PostgreSQL application run.

The shared-port 56215 suite gate was not run in the as-built phase: its `os_pg` fixture
creates roles and schema and revokes database grants, which conflicts with the read-only
restriction on that shared database. Nothing there was connected to or changed. Claude
coordinator review, Linux CI and production acceptance are still pending.
