"""Read-only main-DSN authority probe. Never include database errors in logs."""

import logging

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)

_ROLE_SQL = """
SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls
       OR NOT rolcanlogin AS unsafe,
       (SELECT datdba=to_regrole(current_user) FROM pg_database
        WHERE datname=current_database()) AS owns_database,
       EXISTS(SELECT 1 FROM pg_auth_members WHERE member=to_regrole(current_user)) AS member,
       session_user <> current_user AS impersonated
FROM pg_roles WHERE rolname=current_user
"""

# Keep the effective schema/table/column/sequence/function coverage of V2-14's
# _validate_main_role_catalog. PUBLIC grants count even without schema USAGE.
_LEDGER_SQL = """
SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='assaf_os' AND
has_schema_privilege(current_user, oid, 'USAGE,CREATE')) OR EXISTS(
SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
WHERE n.nspname='assaf_os' AND CASE
WHEN c.relkind='S' THEN has_sequence_privilege(current_user,c.oid,'USAGE,SELECT,UPDATE')
WHEN c.relkind IN ('r','p','v','m','f') THEN
has_table_privilege(current_user,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')
OR has_any_column_privilege(current_user,c.oid,'SELECT,INSERT,UPDATE,REFERENCES')
ELSE false END)
OR EXISTS(SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE n.nspname='assaf_os' AND has_function_privilege(current_user,p.oid,'EXECUTE'))
"""


def inspect_posture(engine: Engine) -> str:
    if engine.dialect.name != "postgresql":
        return "app_role_postgres_required"
    with engine.connect() as conn:
        role = conn.execute(text(_ROLE_SQL)).mappings().one()
        if role["unsafe"]:
            return "app_role_demote_required"
        if role["owns_database"]:
            return "app_role_database_owner_unsafe"
        if role["member"]:
            return "app_role_membership_unsafe"
        if role["impersonated"]:
            return "app_role_impersonation_unsafe"
        if conn.scalar(text(_LEDGER_SQL)):
            return "app_role_ledger_access_unsafe"
    return "app_role_ok"


def posture_ready(engine: Engine, mode: str = "observe") -> bool:
    try:
        reason = inspect_posture(engine)
    except Exception:
        reason = "app_role_check_failed"
    safe = reason == "app_role_ok"
    ready = mode == "observe" or (mode == "enforce" and safe)
    logger.info("app_role_posture reason=%s safe=%s ready=%s", reason, safe, ready)
    return ready


def app_role_ready(mode: str = "observe") -> bool:
    from app.db.session import get_engine

    try:
        engine = get_engine()
    except Exception:
        ready = mode == "observe"
        logger.info("app_role_posture reason=app_role_check_failed safe=False ready=%s", ready)
        return ready
    return posture_ready(engine, mode)
