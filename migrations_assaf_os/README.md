# Assaf OS ledger schema: V2-14 and V2-14b

This is an additive, inactive foundation in schema `assaf_os` inside database `mia`
(owner decision FORK-DB option A). No application handler, worker or health endpoint
uses this schema. Normal `mia-migrate`, including every ordinary deploy, only scans
`migrations/`. The separate allowlist in `app/db/os_ledger_migrate.py` is
PostgreSQL-only and rejects unexpected SQL files and changed applied-file digests.
`ledger_schema_ready(engine)` is an explicitly invoked schema probe, not proof of
runtime-role safety, activation, deployment or owner acceptance.

The PGB `app/prototypes/assaf_os_ledger.py` is the blueprint: `os_local_ledger_*`
becomes `assaf_os.os_*` (`os_time` retains the entity name). Composite workspace
keys, revisions, request identities and append-only audit design remain intact.
The frozen seven-entity JSON contract and parity oracle are unchanged. Monetary
columns hold integer minor units, checked as unscaled NUMERIC so PostgreSQL cannot
silently round fractional input on assignment to BIGINT. V2-16 must normalize input
units explicitly; this migration performs no business-data import or conversion.
The statement and assistant tables are V2-14b's addendum, below.

## V2-14b schema addendum

Three more allow-listed files, applied after `20261015_assaf_os_ledger.sql` in this
order (the order of `MIGRATIONS`, not of the file names), by the same runner and
journal, inside the same single transaction as any other pending file:

1. `20261016_assaf_os_ledger_core_v2.sql`: `os_workspaces.pending_seq` and
   `statement_pepper_fp`; proposals gain `abandoned` (only for `owner_direct`) and
   receipts gain `superseded` and `abandoned`; the receipt undo-shape checks and the
   INSERT-only `reason_codes`; the proposals `(workspace_id, status, kind, created_at)`
   index; and the import key `(workspace_id, authority_epoch, source_digest)`. Each
   replaced V2-14 constraint is dropped by its exact name, so a renamed one aborts the
   whole run. `undo_state` is unchanged and inert.
2. `20261016_assaf_os_statements.sql`: `os_statements`, `os_statement_lines`,
   `os_statement_batch_lines`, `os_merchant_rules` and `os_statement_review_drafts`.
   Personal lines and personal rules keep only keyed identities. Detail lives only in
   `os_statement_batch_lines.detail` until the purge.
3. `20261016_assaf_os_assistant.sql`: `os_assistant_credentials`, `os_assistant_audit`,
   and the foreign key from `os_proposals(workspace_id, credential_id)`.

None of the three files grants, revokes, creates a function or trigger, or writes a
row. The grant map, including V2-14b's columns and the retention role, lives only in
`app/services/os_ledger/role_policy.py` (contract section 18.2). Retention becomes a
LOGIN NOINHERIT role with schema USAGE, SELECT and DELETE on `os_statement_batch_lines`
and `os_assistant_audit`, and SELECT on five `os_proposals` columns. The runtime role
still has no DELETE or TRUNCATE anywhere. `provision_ledger.py` (both modes, so also the
§18.1 `--mode reapply` restore step) re-applies these grants, and V2-15's validator pins
them exactly. Per §22 E6 the database does not enforce retention's row predicates: the
os-retention job runs only `role_policy.RETENTION_DELETE_SQL`, and a test shows those
statements delete nothing outside them. `tests/db/test_assaf_os_ledger_addendum.py`
pins every column, key, CHECK and index, with a rejecting case for every CHECK.

## E37 issue requests

E37 adds `20261024_assaf_os_issue_requests.sql` after the four existing migrations in
the same allowlist and digest journal. `os_assistant_issue_requests` stores the immutable
OS request and its HMAC session binding. `collect_deadline` is generated from `approved_at`
in UTC, so the runtime gets only SELECT, INSERT and UPDATE of `status`, `approved_at`,
`collected_at`, `credential_id`. Every new constraint is pinned in `role_policy.py`.
The migration contains no grants or application-data writes.

## Explicit operator job and production gate

**Do not run in production until V2-14a / issue #151 APP-ROLE-DEMOTE has landed and
its separately approved production role/DSN change has passed.** This commit neither
demotes the live role nor activates the ledger. A NOINHERIT ledger DSN alone does
not protect history from the old superuser main DSN.

After exact-SHA review, CI success and Assaf's explicit go for that SHA, the
operator provisions `/etc/mia/os-ledger-migrate.env` with mode 0600, root ownership,
and only `MIA_OS_LEDGER_MIGRATION_DATABASE_URL` pointing at the privileged migration
account. This file is available only to the optional `os-ledger-migrate` service;
the app service does not receive it. `/etc/mia/runtime.env` must already contain the
real demoted `MIA_DATABASE_URL`. Never substitute a harmless test role for that DSN.

Run `sudo mia os-ledger migrate --approved-sha <exact-40-character-image-sha>`.
The CLI accepts only this subcommand, holds the normal deployment lock, starts no
dependent service, and verifies the approval SHA against the image's build SHA.
The job rejects an absent/different main DSN, a different target database, elevated
main-role attributes, database ownership, role memberships, or existing ledger privileges.
New effective privileges are checked inside the migration transaction before commit,
including grants introduced by default privileges. It performs
no secret fetching, restart, activation, grant to the app, or business-data write.

The migration transaction holds an advisory lock, creates or checks the dedicated
NOLOGIN NOINHERIT `assaf_os_owner`, creates the schema with that owner, revokes PUBLIC
schema access and runs `SET LOCAL ROLE assaf_os_owner`. SQL and its separate digest
journal commit together. Any SQL failure rolls back the entire migration. Re-run
is a no-op only when the stored digest matches. Recovery from a failed transaction
requires correcting the cause and an approved retry; no destructive down migration
is supplied. An already applied inactive schema can remain in place during rollback
of application code.

## V2-15 role hooks

`RUNTIME_ROLE = assaf_os_ledger` and `RUNTIME_DSN_ENV = MIA_OWNER_OS_LEDGER_DATABASE_URL`
reserve the separate runtime identity and DSN. V2-14 creates no runtime login,
password, memberships or grants. V2-15's explicit provisioning plan creates a LOGIN NOINHERIT NOSUPERUSER
NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS role with no memberships and no
database CREATE/TEMP, schema CREATE, object ownership or unsafe callable functions.
It must connect as itself (no SET ROLE), and use the exact privilege-map validator
in `app/services/os_ledger/role_policy.py`. See `ops/assaf-os/LEDGER_ROLE.md`.
`provision_ledger.py --mode reapply` restores this schema's ownership, REVOKEs and
grants after a `--no-owner --no-privileges` restore; on a database without
`assaf_os` it is a no-op. Provisioning grants `assaf_os_owner` to the migration
identity WITH ADMIN TRUE, SET FALSE, INHERIT FALSE, the one edge this runner allows.

The runtime map denies DELETE/TRUNCATE everywhere (V2-14b gives DELETE only to the
retention role, on two tables); events, receipts and audit are INSERT/SELECT only; the
migration journal and authority fence are not runtime-writable.
Record/proposal/outbox updates need their own explicit column allowlists. Runtime
role and main app role must differ. The main DSN must independently fail closed on
superuser/elevated attributes or ledger access, including in authority preflight.
Role safety is not asserted by this foundation or by schema readiness.

## SQL parser deviation and verification

An assistant proposal must be `statement_batch` and pending **at insertion**, while
later owner decisions may change its status. A permanent `CHECK(status='pending')`
would reject valid terminal decisions. The INSERT-only trigger uses a PL/pgSQL body.
Consequently this isolated runner sends the whole reviewed script to PostgreSQL,
without parameters, rather than using the legacy semicolon splitter. This replaces
the original V2-14 no-dollar-quotes/statement-count test with execution of the real
trigger, terminal updates, atomic failure and repeat-run tests. The legacy splitter
and normal migration directory remain unchanged.

`tests/db/test_assaf_os_ledger_migration.py` resets only the explicitly named loopback
`mia_ledger_test`, executes the actual normal worker twice with no ledger schema,
then exercises the explicit migration, guards and all three existing Owner OS role
validators. CI's `os-ledger-migration-postgres` job uses PostgreSQL 16 and gates the container
job. Local red/green evidence lives under ignored `.cache/`.

Binding owner choices outside this schema chunk remain: sessions 12 hours idle /
14 days absolute, and auto-refresh every 3 minutes plus on return. No session or
frontend refresh implementation changes are included here. No claim about the
October/November statement cycle or a cutover date is made by this foundation.
