"""Explicit PostgreSQL-only ledger migration. Never called by normal mia-migrate."""

from __future__ import annotations

import hashlib
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.db.migrate import MigrationSummary

MIGRATIONS = (
    "20261015_assaf_os_ledger.sql",
    # V2-14b schema addendum (V2_PG_CONTRACTS.md section 4), applied in this order.
    "20261016_assaf_os_ledger_core_v2.sql",
    "20261016_assaf_os_statements.sql",
    "20261016_assaf_os_assistant.sql",
    "20261024_assaf_os_issue_requests.sql",
    "20261025_assaf_os_assistant_followups.sql",
    "20261026_assaf_os_oauth.sql",
)
OWNER_ROLE = "assaf_os_owner"
# V2-15 must provision a separate NOINHERIT login and validate its exact grant map.
RUNTIME_ROLE = "assaf_os_ledger"
RUNTIME_DSN_ENV = "MIA_OWNER_OS_LEDGER_DATABASE_URL"
TABLES = frozenset({
    "os_workspaces", "os_authority", "os_leads", "os_clients", "os_works", "os_payments",
    "os_time", "os_expenses", "os_tasks", "os_proposals", "os_receipts", "os_events",
    "os_outbox", "os_mirror_state", "os_summary_monthly", "os_summary_cycle",
    "os_import_batches", "os_import_rows",
    # V2-14b
    "os_statements", "os_statement_lines", "os_statement_batch_lines", "os_merchant_rules",
    "os_statement_review_drafts", "os_assistant_credentials", "os_assistant_audit",
    "os_assistant_issue_requests",
    "os_assistant_oauth_requests",
})


def migration_directory() -> Path:
    for root in (Path(__file__).resolve().parents[2], Path("/app"), Path.cwd()):
        candidate = root / "migrations_assaf_os"
        if candidate.is_dir():
            return candidate
    raise ValueError("ledger_migrations_missing")


def migration_scripts() -> list[tuple[str, str, str]]:
    directory = migration_directory()
    if {path.name for path in directory.glob("*.sql")} != set(MIGRATIONS):
        raise ValueError("ledger_migration_not_allowlisted")
    scripts = []
    for name in MIGRATIONS:
        data = (directory / name).read_bytes()
        if b"\r" in data:
            raise ValueError("ledger_migration_requires_lf")
        scripts.append((name, data.decode("utf-8"), hashlib.sha256(data).hexdigest()))
    return scripts


def apply_ledger_migrations(engine: Engine, *, main_role: str | None = None) -> MigrationSummary:
    """Apply only the reviewed allowlist, atomically, as a non-login schema owner.

    The operator worker additionally requires owner go and APP-ROLE-DEMOTE posture.
    This engine-only entry point is also used by disposable PostgreSQL tests.
    PostgreSQL parses the complete script so trigger bodies are never split on ';'.
    Unlike the historical runner, duplicate or malformed DDL is not swallowed.
    """
    if engine.dialect.name != "postgresql":
        raise ValueError("postgres_required")
    scripts = migration_scripts()
    result = MigrationSummary()
    with engine.begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(734927, 14)"))
        posture = conn.execute(text(
            "SELECT rolcanlogin, rolsuper, rolinherit, rolcreaterole, rolcreatedb, "
            "rolreplication, rolbypassrls FROM pg_roles WHERE rolname=:role"
        ), {"role": OWNER_ROLE}).first()
        if posture is None:
            conn.execute(text(
                "CREATE ROLE assaf_os_owner NOLOGIN NOINHERIT NOSUPERUSER NOCREATEROLE "
                "NOCREATEDB NOREPLICATION NOBYPASSRLS"
            ))
        elif any(posture):
            raise ValueError("ledger_owner_unsafe")
        if conn.scalar(text(
            "SELECT EXISTS(SELECT 1 FROM pg_auth_members "
            "WHERE member='assaf_os_owner'::regrole OR "
            "(roleid='assaf_os_owner'::regrole AND NOT "
            "(member=(SELECT oid FROM pg_roles WHERE rolname = session_user) AND admin_option "
            "AND NOT set_option AND NOT inherit_option)))"
        )):
            raise ValueError("ledger_owner_membership_unsafe")
        conn.execute(text("CREATE SCHEMA IF NOT EXISTS assaf_os AUTHORIZATION assaf_os_owner"))
        if conn.scalar(text(
            "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='assaf_os'"
        )) != OWNER_ROLE:
            raise ValueError("ledger_schema_owner_unsafe")
        conn.execute(text("REVOKE ALL ON SCHEMA assaf_os FROM PUBLIC"))
        # Do not grant the main application or an unreviewed runtime any access.
        conn.execute(text("SET LOCAL ROLE assaf_os_owner"))
        conn.execute(text("SET LOCAL search_path = pg_catalog, assaf_os"))
        conn.execute(text(
            "ALTER DEFAULT PRIVILEGES FOR ROLE assaf_os_owner "
            "REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
        ))
        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS assaf_os.schema_migrations ("
            "filename text PRIMARY KEY, sha256 text NOT NULL, "
            "applied_at timestamptz NOT NULL DEFAULT now())"
        ))
        applied = dict(conn.execute(text(
            "SELECT filename, sha256 FROM assaf_os.schema_migrations"
        )).all())
        if set(applied) - set(MIGRATIONS):
            raise ValueError("ledger_migration_history_unknown")
        for name, sql, digest in scripts:
            if name in applied:
                if applied[name] != digest:
                    raise ValueError("ledger_migration_digest_mismatch")
                result.already.append(name)
                continue
            conn.exec_driver_sql(sql, execution_options={"no_parameters": True})
            conn.execute(text(
                "INSERT INTO assaf_os.schema_migrations(filename, sha256) VALUES (:name, :digest)"
            ), {"name": name, "digest": digest})
            result.applied.append(name)
        if main_role is not None:
            # Default privileges take effect during CREATE. Inspect this transaction's
            # catalog before commit so an unsafe grant rolls back with all of the DDL.
            _validate_main_role_catalog(conn, main_role)
    return result


def _validate_main_role_catalog(conn: Connection, role: str) -> None:
    params = {"role": role}
    unsafe = conn.scalar(text(
        "SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls "
        "OR NOT rolcanlogin FROM pg_roles WHERE rolname=:role"
    ), params)
    if unsafe is not False:
        raise ValueError("app_role_demote_required")
    if conn.scalar(text(
        "SELECT datdba=to_regrole(:role) FROM pg_database WHERE datname=current_database()"
    ), params):
        raise ValueError("app_role_database_owner_unsafe")
    if conn.scalar(text(
        "SELECT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=to_regrole(:role))"
    ), params):
        raise ValueError("app_role_membership_unsafe")
    # Reject privileges even without USAGE: a later schema grant must not unlock them.
    if conn.scalar(text(
        "SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='assaf_os' AND "
        "has_schema_privilege(:role, oid, 'USAGE,CREATE')) OR EXISTS("
        "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='assaf_os' AND CASE "
        "WHEN c.relkind='S' THEN has_sequence_privilege(:role,c.oid,'USAGE,SELECT,UPDATE') "
        "WHEN c.relkind IN ('r','p','v','m','f') THEN "
        "has_table_privilege(:role,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,"
        "REFERENCES,TRIGGER') "
        "OR has_any_column_privilege(:role,c.oid,'SELECT,INSERT,UPDATE,REFERENCES') "
        "ELSE false END) "
        "OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        "WHERE n.nspname='assaf_os' AND has_function_privilege(:role,p.oid,'EXECUTE'))"
    ), params):
        raise ValueError("app_role_ledger_access_unsafe")


def validate_main_role(main_engine: Engine, migration_engine: Engine) -> str:
    """Fail closed until V2-14a's real main DSN is demoted in this same database."""
    if {main_engine.dialect.name, migration_engine.dialect.name} != {"postgresql"}:
        raise ValueError("postgres_required")
    identity_sql = text(
        "SELECT current_database(), inet_server_addr()::text, inet_server_port(), session_user"
    )
    with main_engine.connect() as conn, migration_engine.connect() as migration:
        main_identity = conn.execute(identity_sql).one()
        migration_identity = migration.execute(identity_sql).one()
        if main_identity[:3] != migration_identity[:3] or None in main_identity[:3]:
            raise ValueError("ledger_database_mismatch")
        if main_identity[3] == migration_identity[3]:
            raise ValueError("ledger_main_dsn_not_separate")
        if not conn.scalar(text("SELECT session_user = current_user")):
            raise ValueError("app_role_impersonation_unsafe")
        _validate_main_role_catalog(conn, main_identity[3])
        return main_identity[3]
