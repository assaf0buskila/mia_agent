# V2-15: inactive ledger role contract

The ledger code path cannot delete or rewrite events/receipts. This guarantee
does not cover a superuser main DSN. V2-14a / #151 APP-ROLE-DEMOTE must land and
pass its owner-approved production rollout before ledger activation. This chunk
neither demotes the main role nor applies production grants. Claude's independent
review and CI on the committed SHA remain separate gates.

`MIA_OWNER_OS_LEDGER_DATABASE_URL` is the canonical runtime setting (empty by
default, excluded from Settings repr). The V2-14 placeholder constant now uses
this issue-specified name. Role/table names, SQL migration and ownership are
unchanged. No runtime factory or feature is activated here.

## Grant contract

`ledger_grant_statements()` is the full executable grant list. `TABLE_PRIVILEGES`
and `COLUMN_UPDATES` define it; no default privileges or grant options are granted.

- `assaf_os_owner`: unchanged NOLOGIN, NOINHERIT schema/table/function owner.
  Membership is denied in both directions except ADMIN TRUE, SET FALSE,
  INHERIT FALSE from owner to the explicitly pinned migration identity.
- `assaf_os_ledger`: LOGIN, NOINHERIT, NOSUPERUSER, NOCREATEROLE, NOCREATEDB,
  NOREPLICATION, NOBYPASSRLS; schema USAGE; SELECT on `os_authority`; SELECT/INSERT
  on the other 17 V2-14 tables; UPDATE only on `COLUMN_UPDATES`' named columns;
  EXECUTE only on `assaf_os.expense_source_key_sha256(text)`. No sequence grants.
  Events and receipts have SELECT/INSERT only, including no receipt undo-state
  update. Future undo implementation must append a separate receipt.
- `assaf_os_retention`: NOLOGIN, NOINHERIT, no elevated attributes, memberships or
  grants. The design's privacy critique requires a scheduled purge role for
  terminal `os_statement_batch_lines` and `os_assistant_audit` older than 180 days.
  Neither table exists in V2-14. Reserve the identity but fail closed on all
  grants until those chunks supply a reviewed age/state-constrained interface.
  A bare DELETE grant cannot enforce those row predicates. No purge job here.
- Main DSN and PUBLIC: no privileges on `assaf_os` or its objects. No runtime
  access to `schema_migrations`, schema CREATE, database CREATE/TEMP, object
  ownership, role memberships, or privileges in other application schemas.
  PostgreSQL's ambient public-schema USAGE and catalog functions remain available.
  No DELETE/TRUNCATE grants, no authority INSERT/UPDATE, no invented audit table.

## Coordinator API

Use `validate_ledger_role(db: Session, main_engine: Engine, *, migration_role: str)
-> None` before a ledger operation, using the actual configured ledger session and
actual `MIA_DATABASE_URL` engine. Both identities must be unimpersonated, different,
and on the same PostgreSQL server/database. Pass the pinned privileged migration
identity, never a user-request parameter. Refusal is always
`ValueError("ledger_role_denied")`, including unavailable catalogs. Never catch it
and continue. `mia os-authority preflight` does not exist in this base; its future
coordinator must invoke this same hook. Schema readiness alone is insufficient.

`validate_ledger_catalog(conn: Connection, *, main_role: str, migration_role: str)
-> None` is exclusively an operator transaction hook. It cannot authenticate a
DSN from a role-name string. Provisioning obtains `main_role` by connecting the
real main DSN through V2-14's `validate_main_role` before calling it. It does not
commit. Same sanitized refusal code. Both hooks set transaction-local timeouts
and a catalog-only search path. No posture caching.

Checks include exact table/column/function/schema ACLs and grant options, schema
ownership and object inventory, effective off-schema privileges, all sequences,
PUBLIC, elevated attributes, memberships in both directions, and global plus
schema-local default ACLs. The owner must retain its global PUBLIC EXECUTE revoke;
a schema-local revoke is not a substitute. Unknown tables/functions fail closed.

## Explicit provisioning

From the repo, with `MIA_ENV=test` for disposable work:

    uv run --offline python ops/assaf-os/provision_ledger.py --target local-v2-15

Dry-run is the default and makes no connections. It prints every DDL and GRANT
statement. The only enrolled target is 127.0.0.1:56215 / mia_ledger_test, migration
identity postgres and main identity v215_main. Production is intentionally not
enrolled. Enrolling it requires reviewed exact target pins and a separate owner go.

Apply additionally requires `--apply --approved-sha <40 lowercase hex>`, matching
`MIA_BUILD_SHA`, plus explicit `MIA_OS_LEDGER_MIGRATION_DATABASE_URL` and
`MIA_DATABASE_URL` matching the pins (no URL query overrides). Connections use a
10-second connect timeout. The main-role demotion check precedes DDL; the final
catalog audit happens before transaction commit. Existing runtime/retention roles
cause refusal rather than adoption. Every error rolls back the transaction and
prints only `ledger_provision_refused`; missing SHA approval prints
`ledger_owner_go_sha_required`. Invalid arguments exit 2. No secrets are printed.

The printed plan revokes database TEMPORARY from PUBLIC because a per-role revoke
cannot cancel an inherited PUBLIC privilege. This database-wide impact is explicit
in dry-run and must be considered when a future production plan is approved. The
script assigns no passwords and grants no main-role or retention access.

The existing `os-ledger-migration-postgres` CI job runs the new suite alongside
V2-14's migration and three-validator regression, with its skip guard unchanged.
No live FastAPI ledger path exists yet, so no live-path latency claim is made.
