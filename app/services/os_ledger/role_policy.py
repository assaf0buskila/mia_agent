"""Exact V2-14 PostgreSQL grant map; callers must validate both real DSNs.

No activation or connection is performed on import. The catalog hook is for an
operator transaction; runtime/preflight callers must use validate_ledger_role.
Retention is reserved, NOLOGIN and unprivileged until its tables and a bounded
scheduled purge interface land. No default grants are used for future tables.
"""

from collections.abc import Mapping
from types import MappingProxyType

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.db.os_ledger_migrate import OWNER_ROLE, RUNTIME_ROLE, TABLES, _validate_main_role_catalog

RETENTION_ROLE = "assaf_os_retention"
DENIED = "ledger_role_denied"

TABLE_PRIVILEGES: Mapping[str, frozenset[str]] = MappingProxyType({
    name: frozenset({"SELECT"} if name == "os_authority" else {"SELECT", "INSERT"})
    for name in sorted(TABLES)
})
_RECORD_COLUMNS = (
    "revision", "inputs", "business_date", "updated_at", "deleted_at", "deleted_by_request_id",
)
COLUMN_UPDATES: Mapping[str, tuple[str, ...]] = MappingProxyType({
    "os_workspaces": ("version", "summaries", "updated_at"),
    "os_leads": _RECORD_COLUMNS,
    "os_clients": _RECORD_COLUMNS,
    "os_works": (*_RECORD_COLUMNS, "agreed_amount_minor", "currency"),
    "os_payments": (*_RECORD_COLUMNS, "amount_minor", "currency"),
    "os_time": (*_RECORD_COLUMNS, "minutes"),
    "os_expenses": (*_RECORD_COLUMNS, "amount_minor", "currency"),
    "os_tasks": (*_RECORD_COLUMNS, "person_id", "work_id", "due_date"),
    "os_proposals": ("status",),
    "os_outbox": (
        "status", "attempts", "next_attempt_at", "lease_owner", "lease_generation",
        "leased_at", "last_error_code", "updated_at",
    ),
    "os_mirror_state": (
        "status", "projected_version", "verified_version", "last_attempt_at", "last_verified_at",
        "last_error_code", "watermark", "scan_coverage", "updated_at",
    ),
    "os_summary_monthly": ("version", "summary", "calculated_at"),
    "os_summary_cycle": ("version", "starts_on", "ends_on", "summary", "calculated_at"),
    "os_import_batches": ("status", "quarantine", "completed_at"),
    "os_import_rows": ("record_id", "status", "canonical_record", "quarantine"),
})
FUNCTION_PRIVILEGES = frozenset({"expense_source_key_sha256(text)"})
_FUNCTIONS = FUNCTION_PRIVILEGES | {"require_pending_assistant_proposal_insert()"}
_USER_SCHEMA = (
    "n.nspname NOT IN ('pg_catalog', 'information_schema') "
    "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp_%'"
)


def ledger_grant_statements() -> tuple[str, ...]:
    """Return the complete, deterministic runtime GRANT set (no dynamic SQL names)."""
    statements = ["GRANT USAGE ON SCHEMA assaf_os TO assaf_os_ledger;"]
    for name, privileges in TABLE_PRIVILEGES.items():
        statements.append(
            f"GRANT {', '.join(sorted(privileges))} ON TABLE assaf_os.{name} TO assaf_os_ledger;"
        )
    for name, columns in COLUMN_UPDATES.items():
        statements.append(
            f"GRANT UPDATE ({', '.join(columns)}) ON TABLE assaf_os.{name} TO assaf_os_ledger;"
        )
    statements.append(
        "GRANT EXECUTE ON FUNCTION assaf_os.expense_source_key_sha256(text) TO assaf_os_ledger;"
    )
    return tuple(statements)


def _require(ok: bool) -> None:
    if not ok:
        raise ValueError(DENIED)


def _roles(conn: Connection, main_role: str, migration_role: str) -> None:
    _require(bool(main_role) and bool(migration_role) and len({
        RUNTIME_ROLE, OWNER_ROLE, RETENTION_ROLE, main_role, migration_role,
    }) == 5)
    for role, login in ((RUNTIME_ROLE, True), (OWNER_ROLE, False), (RETENTION_ROLE, False)):
        row = conn.execute(text(
            "SELECT rolcanlogin, rolinherit, rolsuper, rolcreaterole, rolcreatedb, "
            "rolreplication, rolbypassrls FROM pg_roles WHERE rolname=:role"
        ), {"role": role}).first()
        _require(row is not None and row[0] == login and not any(row[1:]))
    _require(conn.scalar(text("SELECT to_regrole(:role) IS NOT NULL"),
                         {"role": migration_role}))
    # Match V2-14 review r1: the owner may ONLY have an ADMIN-only edge to the
    # pinned migration identity. NOINHERIT alone never prevents SET ROLE.
    edges = conn.execute(text(
        "SELECT pg_get_userbyid(roleid), pg_get_userbyid(member), admin_option, "
        "set_option, inherit_option FROM pg_auth_members WHERE "
        "roleid = ANY(SELECT oid FROM pg_roles WHERE rolname = ANY(:roles)) OR "
        "member = ANY(SELECT oid FROM pg_roles WHERE rolname = ANY(:roles))"
    ), {"roles": [RUNTIME_ROLE, OWNER_ROLE, RETENTION_ROLE, main_role]}).all()
    _require(all(tuple(edge) == (OWNER_ROLE, migration_role, True, False, False) for edge in edges))
    _validate_main_role_catalog(conn, main_role)
    for role in (RUNTIME_ROLE, RETENTION_ROLE):
        _require(not conn.scalar(text(
            "SELECT has_database_privilege(:role,current_database(),'CREATE,TEMP') OR EXISTS ("
            "SELECT 1 FROM pg_class WHERE relowner=to_regrole(:role) UNION ALL "
            "SELECT 1 FROM pg_namespace WHERE nspowner=to_regrole(:role) UNION ALL "
            "SELECT 1 FROM pg_proc WHERE proowner=to_regrole(:role) UNION ALL "
            "SELECT 1 FROM pg_database WHERE datdba=to_regrole(:role)) OR EXISTS ("
            "SELECT 1 FROM pg_namespace WHERE has_schema_privilege(:role,oid,'CREATE'))"
        ), {"role": role}))
        _require(not conn.scalar(text("""
            SELECT EXISTS(SELECT 1 FROM pg_namespace n
            CROSS JOIN LATERAL aclexplode(n.nspacl) a
            WHERE a.grantee=to_regrole(:role) AND n.nspname<>'assaf_os')
            OR EXISTS(SELECT 1 FROM pg_database d
            CROSS JOIN LATERAL aclexplode(d.datacl) a
            WHERE d.datname=current_database() AND a.grantee=to_regrole(:role))
        """), {"role": role}))
        _require(not conn.scalar(text(f"""
            SELECT EXISTS(SELECT 1 FROM pg_namespace n WHERE {_USER_SCHEMA}
            AND n.nspname NOT IN ('assaf_os','public')
            AND has_schema_privilege(:role,n.oid,'USAGE'))
        """), {"role": role}))


def _acl(conn: Connection, sql: str, expected: set[tuple]) -> None:
    # A non-owner ACL entry must match exactly, including its grant-option bit.
    # PUBLIC (OID zero) and arbitrary third-party grants are therefore rejected.
    rows = conn.execute(text(sql)).all()
    _require({tuple(row) for row in rows} == expected)


def _catalog(conn: Connection, main_role: str, migration_role: str) -> None:
    _require(conn.dialect.name == "postgresql")
    conn.execute(text("SET LOCAL statement_timeout = '5s'"))
    conn.execute(text("SET LOCAL lock_timeout = '5s'"))
    conn.execute(text("SET LOCAL search_path = pg_catalog"))
    _roles(conn, main_role, migration_role)
    _require(conn.scalar(text(
        "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='assaf_os'"
    )) == OWNER_ROLE)
    relations = conn.execute(text(
        "SELECT c.relname,c.relkind,pg_get_userbyid(c.relowner) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='assaf_os' "
        "AND c.relkind NOT IN ('i','I','t')"
    )).all()
    _require({tuple(row) for row in relations} == {
        (name, "r", OWNER_ROLE) for name in TABLES | {"schema_migrations"}
    })
    functions = conn.execute(text(
        "SELECT p.proname || '(' || pg_get_function_identity_arguments(p.oid) || ')', "
        "pg_get_userbyid(p.proowner), p.prosecdef FROM pg_proc p JOIN pg_namespace n "
        "ON n.oid=p.pronamespace WHERE n.nspname='assaf_os'"
    )).all()
    # identity_arguments includes the declared argument name in this SQL helper.
    _require({(name.replace('value text', 'text'), owner, security) for name, owner, security
              in functions} == {(name, OWNER_ROLE, False) for name in _FUNCTIONS})
    _acl(conn, """
        SELECT pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable
        FROM pg_namespace n CROSS JOIN LATERAL
        aclexplode(COALESCE(n.nspacl,acldefault('n',n.nspowner))) a
        WHERE n.nspname='assaf_os' AND a.grantee<>n.nspowner
    """, {(RUNTIME_ROLE, "USAGE", False)})
    _acl(conn, """
        SELECT c.relname,pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,acldefault(
            CASE WHEN c.relkind='S' THEN 'S'::"char" ELSE 'r'::"char" END,c.relowner))) a
        WHERE n.nspname='assaf_os' AND a.grantee<>c.relowner
    """, {(name, RUNTIME_ROLE, privilege, False)
          for name, privileges in TABLE_PRIVILEGES.items() for privilege in privileges})
    _acl(conn, """
        SELECT c.relname,col.attname,pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        JOIN pg_attribute col ON col.attrelid=c.oid
        CROSS JOIN LATERAL aclexplode(col.attacl) a
        WHERE n.nspname='assaf_os' AND a.grantee<>c.relowner
    """, {(name, col, RUNTIME_ROLE, "UPDATE", False)
          for name, columns in COLUMN_UPDATES.items() for col in columns})
    _acl(conn, """
        SELECT p.proname,pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a
        WHERE n.nspname='assaf_os' AND a.grantee<>p.proowner
    """, {("expense_source_key_sha256", RUNTIME_ROLE, "EXECUTE", False)})
    # Reject ambient PUBLIC privileges and explicit grants in ANY user schema,
    # even without schema USAGE. Retention has no effective object privileges.
    for role in (RUNTIME_ROLE, RETENTION_ROLE):
        _require(not conn.scalar(text(f"""
            SELECT EXISTS(SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE {_USER_SCHEMA} AND (n.nspname<>'assaf_os' OR :role=:retention) AND CASE
              WHEN c.relkind='S' THEN has_sequence_privilege(:role,c.oid,'USAGE,SELECT,UPDATE')
              WHEN c.relkind IN ('r','p','v','m','f') THEN
                has_table_privilege(:role,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
                OR has_any_column_privilege(:role,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')
              ELSE false END)
            OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE {_USER_SCHEMA} AND (n.nspname<>'assaf_os' OR :role=:retention)
            AND has_function_privilege(:role,p.oid,'EXECUTE'))
        """), {"role": role, "retention": RETENTION_ROLE}))
    _defaults(conn, main_role)


def _defaults(conn: Connection, main_role: str) -> None:
    # Absence of a global owner function ACL restores PostgreSQL's implicit
    # PUBLIC EXECUTE. A schema-local REVOKE cannot cancel that default.
    _require(conn.scalar(text("""
        SELECT NOT EXISTS(SELECT 1 FROM aclexplode(COALESCE(
          (SELECT defaclacl FROM pg_default_acl WHERE defaclrole='assaf_os_owner'::regrole
           AND defaclnamespace=0 AND defaclobjtype='f'),
          acldefault('f','assaf_os_owner'::regrole))) a
          WHERE a.grantee<>'assaf_os_owner'::regrole)
    """)))
    _require(not conn.scalar(text("""
        SELECT EXISTS(SELECT 1 FROM pg_default_acl d
        CROSS JOIN LATERAL aclexplode(d.defaclacl) a WHERE
          (d.defaclnamespace=(SELECT oid FROM pg_namespace WHERE nspname='assaf_os')
           AND a.grantee<>d.defaclrole)
          OR (d.defaclrole='assaf_os_owner'::regrole AND a.grantee<>d.defaclrole)
          OR a.grantee IN (0,'assaf_os_ledger'::regrole,'assaf_os_retention'::regrole)
          OR (d.defaclnamespace=0 AND a.grantee=to_regrole(:main)
              AND d.defaclrole<>a.grantee))
    """), {"main": main_role}))


def validate_ledger_catalog(
    conn: Connection, *, main_role: str, migration_role: str,
) -> None:
    """Operator-only transactional audit; raises ValueError('ledger_role_denied').

    main_role must come from validate_main_role on the actual main DSN, never a
    caller-supplied replacement. Runtime consumers use validate_ledger_role below.
    This function does not commit and cannot prove a supplied role is a real DSN.
    """
    try:
        _catalog(conn, main_role, migration_role)
    except (ValueError, SQLAlchemyError, TypeError, AttributeError):
        raise ValueError(DENIED) from None


def validate_ledger_role(db: Session, main_engine: Engine, *, migration_role: str) -> None:
    """Runtime/authority-preflight hook; audit both actual DSNs, without caching.

    Requires separate, unimpersonated identities on the same PostgreSQL server
    and database. All refusals, including unavailable catalogs, use the single
    sanitized ValueError code ledger_role_denied. Does not activate the ledger.
    """
    try:
        _require(db.get_bind().dialect.name == main_engine.dialect.name == "postgresql")
        db.execute(text("SET LOCAL search_path = pg_catalog"))
        identity = text(
            "SELECT current_database(),inet_server_addr()::text,inet_server_port(),"
            "session_user,current_user"
        )
        ledger = db.execute(identity).one()
        _require(ledger[3] == ledger[4] == RUNTIME_ROLE and None not in ledger[:3])
        with main_engine.connect() as main:
            main.execute(text("SET LOCAL search_path = pg_catalog"))
            app = main.execute(identity).one()
            _require(app[:3] == ledger[:3] and app[3] == app[4] and app[3] != ledger[3])
            main.execute(text("SET LOCAL statement_timeout = '5s'"))
            _validate_main_role_catalog(main, app[3])
            validate_ledger_catalog(
                db.connection(), main_role=app[3], migration_role=migration_role,
            )
    except (ValueError, SQLAlchemyError, TypeError, AttributeError):
        raise ValueError(DENIED) from None
