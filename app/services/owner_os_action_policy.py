"""Fail-closed PostgreSQL ACL policy for prepared Owner OS actions.

The action DSN is intentionally separate from read and authentication roles.  This
validator checks effective privileges, including PUBLIC and enabled-role effects,
before every action invocation.  It exposes the exact grant map used by disposable
database fixtures and deployment provisioning.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

_SELECT = frozenset({"SELECT"})
_SELECT_INSERT = frozenset({"SELECT", "INSERT"})
_SELECT_INSERT_UPDATE = frozenset({"SELECT", "INSERT", "UPDATE"})

# Quick-lead proposals/execution plus reminder creation/change. CRM identifiers,
# activities, issues, links, and outbox ids are application-generated strings, so
# only approvals and idempotency records need a database sequence.
ACTION_TABLE_PRIVILEGES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        "approvals": _SELECT_INSERT_UPDATE,
        "idempotency_records": _SELECT_INSERT_UPDATE,
        "crm_contacts": _SELECT_INSERT,
        "crm_identities": _SELECT_INSERT,
        "crm_activities": _SELECT_INSERT,
        "crm_sync_snapshots": _SELECT,
        "crm_issues": _SELECT_INSERT_UPDATE,
        "crm_issue_contacts": _SELECT_INSERT,
        "crm_outbox": _SELECT_INSERT_UPDATE,
        "owner_reminders": _SELECT_INSERT_UPDATE,
    }
)

ACTION_SEQUENCE_PRIVILEGES: Final[Mapping[str, frozenset[str]]] = MappingProxyType(
    {
        # nextval() needs USAGE. INSERT ... RETURNING supplies ids without currval(),
        # so SELECT and UPDATE on either sequence remain prohibited.
        "approvals_id_seq": frozenset({"USAGE"}),
        "idempotency_records_id_seq": frozenset({"USAGE"}),
    }
)

_TABLE_PRIVILEGES = (
    "SELECT",
    "INSERT",
    "UPDATE",
    "DELETE",
    "TRUNCATE",
    "REFERENCES",
    "TRIGGER",
)
_COLUMN_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "REFERENCES")
_SEQUENCE_PRIVILEGES = ("USAGE", "SELECT", "UPDATE")
_SYSTEM_SCHEMAS = ("pg_catalog", "information_schema")
_DENIED = "action_role_denied"


def _allowed(
    *,
    schema: str,
    current_schema: str,
    name: str,
    privilege: str,
    grants: Mapping[str, frozenset[str]],
) -> bool:
    return schema == current_schema and privilege in grants.get(name, frozenset())


def _required_relation_privilege(
    db: Session,
    *,
    name: str,
    privilege: str,
    sequence: bool,
) -> bool:
    checker = "has_sequence_privilege" if sequence else "has_table_privilege"
    relation_kind = "c.relkind = 'S'" if sequence else "c.relkind IN ('r', 'p')"
    return bool(
        db.execute(
            text(
                f"""
                SELECT {relation_kind}
                  AND {checker}(current_user, c.oid, :privilege)
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = current_schema()
                  AND c.relname = :name
                """
            ),
            {"name": name, "privilege": privilege},
        ).scalar_one_or_none()
    )


def _validate_required_privileges(db: Session) -> None:
    for name, privileges in ACTION_TABLE_PRIVILEGES.items():
        for privilege in privileges:
            if not _required_relation_privilege(
                db,
                name=name,
                privilege=privilege,
                sequence=False,
            ):
                raise ValueError(_DENIED)
    for name, privileges in ACTION_SEQUENCE_PRIVILEGES.items():
        for privilege in privileges:
            if not _required_relation_privilege(
                db,
                name=name,
                privilege=privilege,
                sequence=True,
            ):
                raise ValueError(_DENIED)


def _validate_effective_relation_privileges(db: Session, current_schema: str) -> None:
    table_rows = db.execute(
        text(
            """
            SELECT n.nspname, c.relname, p.privilege
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN (VALUES
              ('SELECT'), ('INSERT'), ('UPDATE'), ('DELETE'),
              ('TRUNCATE'), ('REFERENCES'), ('TRIGGER')
            ) AS p(privilege)
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%'
              AND n.nspname NOT LIKE 'pg_temp_%'
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND has_table_privilege(current_user, c.oid, p.privilege)
            """
        )
    ).all()
    if any(
        not _allowed(
            schema=str(schema),
            current_schema=current_schema,
            name=str(name),
            privilege=str(privilege),
            grants=ACTION_TABLE_PRIVILEGES,
        )
        for schema, name, privilege in table_rows
    ):
        raise ValueError(_DENIED)

    column_rows = db.execute(
        text(
            """
            SELECT n.nspname, c.relname, p.privilege
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            JOIN pg_attribute a ON a.attrelid = c.oid
            CROSS JOIN (VALUES
              ('SELECT'), ('INSERT'), ('UPDATE'), ('REFERENCES')
            ) AS p(privilege)
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%'
              AND n.nspname NOT LIKE 'pg_temp_%'
              AND c.relkind IN ('r', 'p', 'v', 'm', 'f')
              AND a.attnum > 0
              AND NOT a.attisdropped
              AND has_column_privilege(current_user, c.oid, a.attnum, p.privilege)
            """
        )
    ).all()
    if any(
        not _allowed(
            schema=str(schema),
            current_schema=current_schema,
            name=str(name),
            privilege=str(privilege),
            grants=ACTION_TABLE_PRIVILEGES,
        )
        for schema, name, privilege in column_rows
    ):
        raise ValueError(_DENIED)

    sequence_rows = db.execute(
        text(
            """
            SELECT n.nspname, c.relname, p.privilege
            FROM pg_class c
            JOIN pg_namespace n ON n.oid = c.relnamespace
            CROSS JOIN (VALUES ('USAGE'), ('SELECT'), ('UPDATE')) AS p(privilege)
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
              AND n.nspname NOT LIKE 'pg_toast%'
              AND n.nspname NOT LIKE 'pg_temp_%'
              AND c.relkind = 'S'
              AND has_sequence_privilege(current_user, c.oid, p.privilege)
            """
        )
    ).all()
    if any(
        not _allowed(
            schema=str(schema),
            current_schema=current_schema,
            name=str(name),
            privilege=str(privilege),
            grants=ACTION_SEQUENCE_PRIVILEGES,
        )
        for schema, name, privilege in sequence_rows
    ):
        raise ValueError(_DENIED)


def _validate_role_posture(db: Session) -> str:
    role = db.execute(
        text(
            """
            SELECT rolsuper, rolcreaterole, rolcreatedb, rolreplication,
                   rolbypassrls, rolinherit, rolcanlogin
            FROM pg_roles
            WHERE rolname = current_user
            """
        )
    ).first()
    if role is None or any(bool(value) for value in role[:6]) or not bool(role[6]):
        raise ValueError(_DENIED)

    posture = db.execute(
        text(
            """
            SELECT
              current_schema(),
              session_user = current_user,
              NOT has_database_privilege(current_user, current_database(), 'CREATE'),
              NOT has_database_privilege(current_user, current_database(), 'TEMP'),
              NOT EXISTS (
                SELECT 1 FROM pg_auth_members
                WHERE member = (SELECT oid FROM pg_roles WHERE rolname = current_user)
              ),
              NOT EXISTS (
                SELECT 1 FROM pg_namespace
                WHERE nspname NOT IN ('pg_catalog', 'information_schema')
                  AND nspname NOT LIKE 'pg_toast%'
                  AND nspname NOT LIKE 'pg_temp_%'
                  AND has_schema_privilege(current_user, oid, 'CREATE')
              ),
              NOT EXISTS (
                SELECT 1
                FROM pg_proc p
                JOIN pg_namespace n ON n.oid = p.pronamespace
                WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                  AND n.nspname NOT LIKE 'pg_toast%'
                  AND n.nspname NOT LIKE 'pg_temp_%'
                  AND has_function_privilege(current_user, p.oid, 'EXECUTE')
              ),
              NOT EXISTS (
                SELECT 1 FROM pg_class WHERE relowner = current_user::regrole
                UNION ALL
                SELECT 1 FROM pg_namespace WHERE nspowner = current_user::regrole
                UNION ALL
                SELECT 1 FROM pg_proc WHERE proowner = current_user::regrole
                UNION ALL
                SELECT 1 FROM pg_database WHERE datdba = current_user::regrole
              )
            """
        )
    ).first()
    if (
        posture is None
        or not isinstance(posture[0], str)
        or not posture[0]
        or not all(bool(value) for value in posture[1:])
    ):
        raise ValueError(_DENIED)
    return posture[0]


def validate_action_role(db: Session) -> None:
    """Accept only the exact effective ACL needed by the two prepared actions."""
    try:
        if db.get_bind().dialect.name != "postgresql":
            raise ValueError(_DENIED)
        db.execute(text("SET LOCAL statement_timeout = '5s'"))
        db.execute(text("SET LOCAL lock_timeout = '5s'"))
        current_schema = _validate_role_posture(db)
        _validate_effective_relation_privileges(db, current_schema)
        _validate_required_privileges(db)
    except ValueError:
        raise
    except (SQLAlchemyError, TypeError, AttributeError):
        raise ValueError(_DENIED) from None
