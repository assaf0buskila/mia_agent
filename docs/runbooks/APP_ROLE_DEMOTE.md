# V2-14a: owner-gated main application role cutover

Status: implementation and disposable local evidence only. No production commands in
this runbook have been executed by the builder. Independent Claude HEAVY review and
Assaf's approval are required before production. Issue #151, epic #153; companion
V2-14 ledger PR #157 is separate and must not be enabled by this procedure.

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
2. `sudo /opt/mia/app-role-demote/app-role-demote.sh --check`. This only reads the
   catalog/runtime file and prints booleans. Before provisioning, `role_exists=false`
   and `active_is_app=false` are expected. A successful command exit means the check
   ran, not that every posture boolean is safe. Pause new jobs with `sudo mia jobs
   pause` and wait for existing `mia-job@mia-due-scan` and
   `mia-job@mia-ingest-knowledge` units to finish. Preserve an already-paused state.
3. `sudo /opt/mia/app-role-demote/app-role-demote.sh`. This transactionally provisions
   ownership/ACLs and stages `/etc/mia/app-role.staged.env`. The original superuser
   DSN is saved once to `/etc/mia/app-role.rollback.env`. The active runtime file is
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
current `--no-owner --no-privileges` behavior recreates objects as `mia` and strips
ACLs; full disaster recovery requires independently restoring Owner OS/ledger roles
and ACLs before reopening traffic. Default grants cannot replace ownership.

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

Baseline: `.cache/v2-14a-baseline.log`: **3672 passed, 213 skipped**.
Tests-first red: `.cache/v2-14a-red.log`: **22 failed** (missing setting/module/SQL);
`.cache/v2-14a-script-red.log`: **5 errors** (missing helper).
Final local gates (Windows, offline venv):

| Gate | Result | Log |
| --- | --- | --- |
| Full default suite | 3699 passed, 228 skipped | `.cache/v2-14a-full.log` |
| Full suite with Owner OS DB on disposable **56216** | 3737 passed, 190 skipped | `.cache/v2-14a-owner-full.log` |
| Focused role, provisioning, posture and migration suite | 57 passed, zero skipped | `.cache/v2-14a-pg.log` |
| Ruff (`app tests ops`) | passed | `.cache/v2-14a-ruff.log` |
| Widget behavioral checks | passed | `.cache/v2-14a-widget.log` |
| Shell syntax, LF, diff whitespace | passed | `.cache/v2-14a-shell.log`, `.cache/v2-14a-lf.log`, `.cache/v2-14a-diff-check.log` |

The default suite's 228 skips include 15 new PG cases whose opt-in URL is unset,
plus the baseline's 213 opt-in skips. The focused run enables all 15 new PG cases.
An additional fresh HEAVY source review found a POSIX test-fixture ownership issue;
it was corrected and re-reviewed with no remaining blockers. Production file
ownership checks remain intact; separate tests cover their acceptance/rejection.
Claude coordinator review, Linux CI and production acceptance are still pending.

All logs are local and uncommitted. The test fixture allows only the supplied disposable admin URL at
`127.0.0.1:56216/mia_role_test`; it creates unique databases/roles and cleans them up.
It executes the exact V2-14 catalog SQL, the deployment migration worker with
MIA_DATABASE_URL changed to the demoted login, pending ALTER/CREATE migrations,
real catalog staging/rerun/cutover/rollback, and sanitized failure/readiness tests.
Loopback trust authentication proves SQL authority and orchestration, not production
password-auth configuration; the VM helper separately verifies TCP login on cutover.

The full suite's existing `tests/conftest.py` overwrites MIA_DATABASE_URL with SQLite.
The dedicated test explicitly sets it after conftest and resets the engine, so its
real migration proof is not masked by that fixture. General SQLite suite results are
not claimed as a full PostgreSQL application run.

The requested shared-port 56215 suite gate is **not run**: its existing `os_pg`
fixture creates roles/schema and revokes database grants. That conflicts with the
read-only restriction on that shared database. No connection or mutation there was
made. As a safe substitute, the full Owner OS gate used a newly created
`owner_os_test` database on **56216**; the runner refused any pre-existing database
of that name and removed its database after completion. This enabled 38 additional
tests. Cleanup is recorded in `.cache/v2-14a-owner-runner.log`. This does not claim
the exact shared-port gate ran. The issue read via `gh issue view 151 ... --comments` was blocked by sandbox
network policy; implementation used the supplied brief and the local V2-14 reference.
