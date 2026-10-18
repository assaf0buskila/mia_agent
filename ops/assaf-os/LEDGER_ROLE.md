# V2-15: inactive ledger role contract

Binding spec: `V2_PG_CONTRACTS.md` §18.2 (grant map), §5.5 (runtime guard) and §18.1
(restore re-apply). Nothing here activates the ledger, demotes the main role or
touches production. Production provisioning is FORK-PROD-ORDER step 4, after the
V2-14a demote and `mia os-ledger migrate`, with its own owner go.

## What is guaranteed, precisely

- The runtime role `assaf_os_ledger` holds no DELETE, TRUNCATE, REFERENCES or TRIGGER
  anywhere. It cannot UPDATE `os_events`, `os_receipts` (including the inert
  `undo_state`), `os_authority`, `schema_migrations`, `os_workspaces.epoch`, or any
  `workspace_id`, `record_id` or `created_at`. **Events and receipts are therefore
  append-only for the runtime role**; a future undo appends a new receipt.
- **State columns are not monotonic in the database.** The runtime role may UPDATE
  the §18.2 columns, so it can, for example, rewind a record's `revision` or move a
  proposal from `rejected` to `committed`. Their correctness is the ledger core's job
  (V2-16), with events and receipts as the audit trail.
- None of this holds against a superuser or otherwise privileged main DSN. V2-14a's
  demote must be live first, and the validator refuses until it is.

## Roles

- `assaf_os_owner`: NOLOGIN NOINHERIT owner of `assaf_os` and every object in it
  (V2-14). Its only membership edge is ADMIN TRUE, SET FALSE, INHERIT FALSE to the
  migration identity. That single edge is how §5.5 derives the migration identity:
  zero or several such members are refused.
- `assaf_os_ledger`: LOGIN NOINHERIT NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION
  NOBYPASSRLS, no memberships, no TEMP, no CREATE anywhere, no role settings. DSN:
  `MIA_OWNER_OS_LEDGER_DATABASE_URL` (FORK-LEDGER-DSN), only in VM-owned
  `/etc/mia/runtime.env`, never in `mia-env`.
- `assaf_os_retention`: reserved, NOLOGIN, no privileges, no memberships, no settings.
  V2-14b turns it into a LOGIN role when its tables exist.
- The main role: nothing on `assaf_os` (V2-14's `_validate_main_role_catalog`), no TEMP,
  and no parameter ACL.

## Grant map (`role_policy.py`)

`ledger_grant_statements()` is the complete runtime GRANT list, built from
`TABLE_PRIVILEGES` and `COLUMN_UPDATES` (literal tuples, each pinned by a test to the
migration SQL and to the live catalog):

- USAGE on the schema; EXECUTE on `assaf_os.expense_source_key_sha256(text)` only.
- SELECT on `os_authority` and `os_workspaces`; SELECT and INSERT on the other 16 tables.
  The runtime role never creates a workspace (§18.2, §5.4): the privileged
  `AuthorityAdmin.ensure_workspace` does, once, from `mia os-authority init`, before any
  import.
- UPDATE on the record tables: `revision, inputs, business_date, source, updated_at,
  deleted_at, deleted_by_request_id`, plus `os_leads (person_id)`, `os_works
  (person_id, agreed_amount_minor, currency)`, `os_payments (person_id, work_id,
  amount_minor, currency, original_payment_id)`, `os_time (person_id, work_id,
  minutes)`, `os_expenses (amount_minor, currency, source_type, source_key,
  source_digest)` and `os_tasks (person_id, work_id, due_date)`. `source` is needed
  because the prototype's apply rewrites it on every owner edit.
- UPDATE on the other tables exactly as §18.2: `os_workspaces (version, summaries,
  updated_at)`, `os_proposals (status)`, the eight `os_outbox` and nine
  `os_mirror_state` columns, `os_summary_monthly`, `os_summary_cycle`,
  `os_import_batches (status, quarantine, completed_at)` and `os_import_rows
  (record_id, status, canonical_record, quarantine)`. §22 E1-rev's
  `set_import_status` needs exactly the batch columns; import rows stay insert-only
  through the typed API.

## Validator

`validate_ledger_role(db, main_engine, *, migration_role=None)` is the §5.5 runtime
hook. It is a **preflight, not a per-request check** (0.1 to 0.4 s of catalog
queries): run it on the first connection, every 10 minutes and after a reconnect,
and cache the result. It needs the real ledger session and the real main engine:
both unimpersonated, different, and on the same server and database.
`validate_ledger_catalog(conn, *, main_role, migration_role=None)` is the operator
hook used inside provisioning's transaction. An explicit `migration_role` must equal
the derived one. Every refusal, including an unavailable catalog, is
`ValueError("ledger_role_denied")`. Both set a transaction-local
`search_path = pg_catalog, pg_temp`, so a temporary table can never shadow a catalog.

It refuses anything beyond the exact posture:
- table, column, schema, sequence and function ACLs, including grant options, PUBLIC
  and third-party grants; ownership and the exact object inventory; elevated role
  attributes; memberships in either direction; default ACLs (the owner keeps its
  global PUBLIC EXECUTE revoke);
- any rewrite rule on an `assaf_os` relation; any non-internal trigger other than the
  enabled `os_proposals_assistant_insert_pending`, which must also have no WHEN clause,
  no arguments and no column list; and row-level security or policies;
- a foreign key that is not plain: each must be `ON UPDATE NO ACTION ON DELETE NO ACTION`,
  `MATCH SIMPLE`, not deferrable and validated, and every internal (RI) trigger on an
  `assaf_os` relation must be enabled (`tgenabled = 'O'`). The RI triggers run as the
  owner of the referencing table, so one owner-level `ADD CONSTRAINT ... ON UPDATE
  CASCADE` would let the §18.2-permitted UPDATE of `os_leads.person_id` rewrite
  `os_receipts.proposal_id`, which the runtime role may not UPDATE, and `ALTER TABLE ...
  DISABLE TRIGGER` on an RI trigger would let it insert an event with no receipt (the same
  foreign-key bypass that `session_replication_role` is refused for). This rule is generic:
  it holds for any foreign key a later migration adds, whatever the pins say;
- a relation that is not permanent: `SET UNLOGGED` on a table or index makes crash recovery
  truncate it, which would silently lose the append-only events and receipts and the
  outbox that exactly-once depends on, whatever `fsync` and `synchronous_commit` say;
- the constraint and index inventory, below;
- either `assaf_os` function (`expense_source_key_sha256(text)`,
  `require_pending_assistant_proposal_insert()`) whose body, language, volatility,
  strictness, return type or own SET clauses differ from the V2-14 SQL (`FUNCTION_PINS`,
  tied to the migration file by a test): `CREATE OR REPLACE` keeps the owner and the ACL,
  and `ALTER FUNCTION ... SET search_path` changes how the body resolves names;
- any dependency crossing the schema boundary: an outside view, materialized view,
  SQL-body function, foreign key, row-type column, statistics object or publication
  that depends on `assaf_os`, and any `assaf_os` default, check, index or trigger that
  calls a non-system object outside it;
- any object owned by `assaf_os_owner` in this database outside `assaf_os` (other
  than its own default ACL), or any shared object it owns;
- any ACL entry or ownership of the runtime role outside `assaf_os` in any database,
  or on a shared object (tablespace, another database, parameter, large object),
  except CONNECT on this database; and any footprint at all of retention;
- `pg_parameter_acl` entries (SET or ALTER SYSTEM) for PUBLIC, the main, runtime,
  retention or owner role; `pg_db_role_setting` rows for the ledger roles; and a
  database- or cluster-wide `session_replication_role`;
- any user-created SECURITY DEFINER routine owned by a superuser or the owner, in any
  schema and whoever holds EXECUTE (a trigger on a main-owned table or an event
  trigger runs it without an EXECUTE check);
- any SECURITY DEFINER routine whose non-superuser owner holds any privilege on an
  `assaf_os` relation, however it got it;
- membership of any non-superuser role in the cluster (LOGIN or not, with or without
  INHERIT, directly or through other roles) in `pg_read_all_data`, `pg_write_all_data`,
  `pg_maintain` (PostgreSQL 17 and later), `pg_read_server_files`,
  `pg_write_server_files` or `pg_execute_server_program`. These give row access, or
  access to the data files and the server's OS user, with no ACL entry, ownership or
  `pg_depend` edge on `assaf_os`, so none of the checks above sees them. A backup or
  monitoring identity that needs them must be the superuser `mia`, not a second role.
  Monitoring roles such as `pg_monitor` are not affected;
- a ledger session whose `session_replication_role` is not `origin`
  (`validate_ledger_role` only). `ALTER SYSTEM` or `postgresql.conf` can set it
  cluster-wide, which the role- and database-setting checks cannot see, and `replica`
  switches off the foreign keys and the assistant trigger;
- TEMP for the main role.

Out of scope: what a superuser installs outside these catalog paths, such as a
dblink or postgres_fdw loopback with privileged credentials, a `FOR ALL TABLES`
publication, or a logical-replication subscription. A role with the REPLICATION
attribute can stream the WAL and is likewise outside what the ledger can police
(`pg_hba.conf` decides who may connect as one). The main DSN is never a superuser
(V2-14a). Column definitions (type, NOT NULL, default) are not pinned: a weakened column is
an integrity regression, not a privilege path.

**Constraint and index inventory (`CONSTRAINT_PINS`, `INDEX_PINS`).** The database is the
exactly-once and append-only backstop, so its constraints are an exact inventory, not a
count: all 169 (19 primary keys, 12 UNIQUE, 26 foreign keys, 112 CHECKs, including
`schema_migrations`) by table, name and server-deparsed definition, plus the ten indexes
that back no primary key or UNIQUE constraint, by `pg_get_indexdef`. A dropped, renamed,
re-targeted or added constraint or index, a NOT VALID constraint, and an invalid index
are refused. The V2-14 SQL is tied to the pins by a test (keys and foreign keys are derived
from the SQL text; the CHECKs are counted per table and compared with a freshly migrated
catalog).

- **17 CHECKs are pinned by name only** (`definition=None`): the enumerations over a
  `varchar` column (`status IN (...)`, `currency IN (...)`, `kind IN (...)`, ...). PostgreSQL
  deparses `= ANY ((ARRAY['a'::character varying, ...])::text[])` and, after the constraint
  is re-created from that text, which is what every `pg_dump` and `pg_restore` does,
  deparses it as `= ANY (ARRAY[('a'::character varying)::text, ...])`. Pinning the first
  form would refuse a correctly restored database at V2-10's reapply step. A test drops and
  re-adds every constraint from its own live text (twice) and asserts that exactly these 17
  change, once, and that the validator accepts the restored schema. The residual: a
  same-name replacement of one of the 17 by a weaker expression is not detected.
- **Obligation for V2-14b.** A new or changed constraint or index is added to the pins on
  purpose, in the same commit (a failing test prints the difference). A CHECK that is a
  `varchar` enumeration, such as the extended `os_proposals_status_check`, is `None`; the
  roundtrip test says whether a new one must be. The generic foreign-key rule above applies
  to its new foreign keys without any pin, so a new `ON DELETE CASCADE` edge is refused
  until the rule itself is changed on purpose.
- **PostgreSQL upgrades.** The pinned text is PostgreSQL 16's deparse (the VM and CI both
  run 16). After a major upgrade the validator may refuse the migrated database; that is
  the signal to regenerate the pins from a fresh migration on the new version, as part of
  the upgrade's rehearsal.

**Obligation for V2-16 (ledger core): `search_path`.** The validator pins its own session
to `pg_catalog, pg_temp`, but a database-wide `ALTER DATABASE ... SET search_path` is
accepted, and the V2-14 trigger function has no `SET search_path` of its own, so it
resolves operators in the invoker's path. The validator does not assert the ledger
session's `search_path`: production's default path contains `public`, where the main role
holds CREATE (§18.1), so a literal "no schema the main role can CREATE in" check would
refuse every production start. Instead, every ledger transaction must run
`SET LOCAL search_path = pg_catalog, pg_temp` and schema-qualify its SQL (`assaf_os.*`).

## Provisioning (`provision_ledger.py`)

    uv run --offline python ops/assaf-os/provision_ledger.py --target <pin> [--mode reapply]

Dry-run is the default. It connects to nothing and prints the exact plan, including
every REVOKE, ALTER OWNER and GRANT. The plan revokes database TEMP from PUBLIC
(a per-role revoke cannot cancel it), and grants CONNECT to the runtime role and the
ADMIN-only owner edge to the pinned migration identity.

Pinned targets (host, port, database, migration identity, main identity, env file):
- `local-v2-15`: `127.0.0.1:56215/mia_ledger_test`, `postgres`, `v215_main`, no file;
- `mia-vm`: `127.0.0.1:5432/mia`, `mia`, `mia_app`, `/etc/mia/runtime.env`.

`--apply` needs `--approved-sha` equal to `MIA_BUILD_SHA`, plus
`MIA_OS_LEDGER_MIGRATION_DATABASE_URL` and `MIA_DATABASE_URL` that match the pin
exactly (a bare `postgresql://` scheme is accepted; no query parameters). Everything
runs in one transaction under the V2-14 advisory lock. The plan executes, then
`validate_ledger_catalog` audits the transaction's own catalog view, including the
main role's demotion posture, before commit. After commit, a fresh connection with
the runtime DSN must pass `validate_ledger_role`. Any failure prints only
`ledger_provision_refused` (or `ledger_owner_go_sha_required`) and exits 1. A refusal
before commit leaves nothing behind. If only the post-commit check fails, the committed
posture is the one the in-transaction audit accepted. Fix the cause (usually the
runtime DSN or authentication), then re-run `--mode reapply`, which is idempotent.
The main DSN's posture is checked inside the transaction rather than before it, so a
restore's PUBLIC EXECUTE on `assaf_os` functions, which the plan revokes, cannot
block a reapply.

- `--mode provision` refuses an existing runtime or retention role; it never adopts one.
- `--mode reapply` (the §18.1 restore post-step) creates only missing roles, never
  changes an existing role's password, re-owns `assaf_os` and all of its objects,
  re-applies every REVOKE and GRANT and the owner's default ACL, and validates. It is
  idempotent. On a database without `assaf_os` it prints the no-op line, exits 0, and
  does not need the main DSN.
- `--mode reapply` never writes `/etc/mia/runtime.env` (§22 E10: roles are cluster-global
  and survive a restore, so a missing runtime role is an anomaly, and the V2-10 container
  mounts `/etc/mia` read-only). If the runtime role is missing and no DSN is staged, it is
  refused before the `CREATE ROLE` (`ledger_provision_refused`, nothing created, the file
  untouched): run `--mode provision`. If a DSN is already staged, the role is recreated
  with that password and the file is left as it is.

**Runtime DSN (`mia-vm`).** When the runtime role is created, the password is 32
random bytes (hex). Its DSN
`MIA_OWNER_OS_LEDGER_DATABASE_URL=postgresql://assaf_os_ledger:<password>@127.0.0.1:5432/mia`
is staged in `/etc/mia/runtime.env` **before** commit, atomically (temp file, fsync,
mode 0600), keeping every other line. A retry reuses a staged DSN, so a failed
transaction never loses the credential. The env file must be a regular file owned by
the running user (root on the VM) with no group or other access. A second, malformed
or differently pinned ledger line is refused. The password reaches PostgreSQL only as
a SCRAM-SHA-256 verifier computed client-side by libpq, so the plaintext is never in
the statement log. Nothing prints the DSN, the password or the verifier. Ops wiring
(running this script as root with `/etc/mia` writable, from the restore post-step or
the FORK-PROD-ORDER runbook) belongs to V2-10 and the runbook.

**A refused provision leaves its staged DSN behind (operator rule).** The DSN is staged
before commit on purpose, so a transaction that the in-transaction audit then refuses
(`ledger_provision_refused`) rolls the role back but keeps the line in `runtime.env`. That
line now names a role that does not exist. V2-17's `core_authority_state` treats any set
`MIA_OWNER_OS_LEDGER_DATABASE_URL` as live, so after the next app recreate every Sheets
write would fail closed with `ledger_unavailable`. After a refused `--mode provision`,
either fix the cause and re-run it until it passes (the staged password is reused), or
delete the `MIA_OWNER_OS_LEDGER_DATABASE_URL` line before any recreate of the app. The
FORK-PROD-ORDER runbook (step 4) must carry the same sentence.

**Locking.** The staging write is a read-modify-replace of `/etc/mia/runtime.env`, and
`app_role_demote.py` edits the same file, so two runs at once would let the last writer
win and lose either its posture change or the staged ledger DSN. The caller must hold
`/var/lock/mia-state.lock`, as `mia restore`, the V2-10 post-restore step, `mia os-ledger`
and the FORK-PROD-ORDER runbook do. The script does not take that lock itself, on purpose:
those callers already hold it, and a second `flock` on it from inside a locked caller
blocks forever. Run `--apply` only through those wrappers and never alongside
`app-role-demote.sh`. (`--mode reapply` never writes the file, see above.)

## Test fixtures

`tests/services/test_os_ledger_role_policy.py` uses only the disposable database named
by `MIA_OS_LEDGER_TEST_POSTGRES_URL` (`mia_ledger_test` as `postgres`). It never
adopts a pre-existing ledger role or owner edge, and it restores PUBLIC TEMP, the edge
and the roles on teardown. The §3.4 conformance fixture applies
`ledger_grant_statements()` directly. For the §5.5 guard to pass it must also grant
`assaf_os_owner TO <migration identity> WITH ADMIN TRUE, SET FALSE, INHERIT FALSE`,
revoke TEMP from PUBLIC, and use a demoted main role.
