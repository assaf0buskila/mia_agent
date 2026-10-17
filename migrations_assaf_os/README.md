# Assaf OS ledger schema: V2-14

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
Statement-domain and assistant-domain tables belong to their later chunks.

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

`RUNTIME_ROLE = assaf_os_ledger` and `RUNTIME_DSN_ENV = MIA_OS_LEDGER_DATABASE_URL`
reserve the separate runtime identity and DSN. This chunk creates no runtime login,
password, memberships or grants. V2-15 must provision a LOGIN NOINHERIT NOSUPERUSER
NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS role with no memberships and no
database CREATE/TEMP, schema CREATE, object ownership or unsafe callable functions.
It must connect as itself (no SET ROLE), and use an exact privilege-map validator.

The future map must deny DELETE/TRUNCATE everywhere; events, receipts and audit are
INSERT/SELECT only; the migration journal and authority fence are not runtime-writable.
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
