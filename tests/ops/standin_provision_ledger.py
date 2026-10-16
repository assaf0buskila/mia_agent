"""STAND-IN for ops/assaf-os/provision_ledger.py --mode reapply (V2-15 r2). TEST SUPPORT ONLY.

V2-15 r2 (draft PR #170, head 9ad4939) is not on master, so the V2-10 restore drill cannot run the
real file. This stand-in keeps the interface and the statement ORDER of the real reapply plan
(V2_PG_CONTRACTS.md 18.1, read from 9ad4939), on a small stand-in schema, so the drill exercises
`mia restore`'s ordering and abort logic against a real PostgreSQL:

  CLI       --target mia-vm --mode reapply --apply --approved-sha <sha>; env MIA_BUILD_SHA,
            MIA_OS_LEDGER_MIGRATION_DATABASE_URL (superuser mia), MIA_DATABASE_URL (mia_app).
  success   prints the executed statements, exit 0. Nothing prints a DSN or a password.
  no-op     "-- assaf_os is absent: reapply is a no-op", exit 0, main DSN not needed.
  refusal   stderr `ledger_provision_refused` (or `ledger_owner_go_sha_required`), exit 1.
  plan      create only MISSING roles (never touch a password), TEMP/CONNECT, owner edge, re-own the
            schema, tables and functions to assaf_os_owner, REVOKE ALL on routines/tables/sequences/
            schema from PUBLIC and the ledger roles, re-create the owner's default ACL, the exact
            grants, then validate (including V2-14's real _validate_main_role_catalog).

Differences from the real file, all deliberate: a two-table, two-function schema; the grant map is
three lines; no password staging in runtime.env, only the failure of that write when the caller says
/etc/mia is read-only (V210_ETC_MIA_READONLY=1, set by the drill from the mount flag); the pinned
port comes from V210_STANDIN_PORT (the disposable server) instead of 5432; V210_STANDIN_FAIL=1
forces a refusal after the plan ran.
Delete this file when r2 merges and point tests/ops/test_restore_reapply.py at the real script.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db.os_ledger_migrate import _validate_main_role_catalog  # noqa: E402

OWNER = "assaf_os_owner"
RUNTIME = "assaf_os_ledger"
RETENTION = "assaf_os_retention"
NOOP = "-- assaf_os is absent: reapply is a no-op"
OWNED_TABLES = ("os_workspaces", "os_statement_batch_lines")
OWNED_FUNCTIONS = ("os_version()", "os_internal()")
POSTURE = "NOINHERIT NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS"
ROLE_PLAN = (
    (OWNER, f"CREATE ROLE {OWNER} NOLOGIN {POSTURE};"),
    (RUNTIME, f"CREATE ROLE {RUNTIME} LOGIN {POSTURE};"),
    (RETENTION, f"CREATE ROLE {RETENTION} NOLOGIN {POSTURE};"),
)
GRANTEES = f"PUBLIC, {RUNTIME}, {RETENTION}"
# Stand-in for ledger_grant_statements(): the ledger role reads and appends, nothing else.
GRANTS = (
    f"GRANT USAGE ON SCHEMA assaf_os TO {RUNTIME};",
    f"GRANT SELECT, INSERT ON assaf_os.os_workspaces TO {RUNTIME};",
    f"GRANT EXECUTE ON FUNCTION assaf_os.os_version() TO {RUNTIME};",
)
EXPECTED_LEDGER_PRIVILEGES = {
    ("schema", "assaf_os", "USAGE"),
    ("table", "os_workspaces", "SELECT"),
    ("table", "os_workspaces", "INSERT"),
    ("function", "os_version()", "EXECUTE"),
}


def statements(database: str, migration_role: str) -> tuple[str, ...]:
    return (
        *(statement for _, statement in ROLE_PLAN),
        f"REVOKE TEMPORARY ON DATABASE {database} FROM PUBLIC;",
        f"REVOKE ALL ON DATABASE {database} FROM {RUNTIME}, {RETENTION};",
        f"GRANT CONNECT ON DATABASE {database} TO {RUNTIME};",
        f"GRANT {OWNER} TO {migration_role} WITH ADMIN TRUE, SET FALSE, INHERIT FALSE;",
        # A --no-owner restore leaves everything owned by the restoring superuser.
        f"ALTER SCHEMA assaf_os OWNER TO {OWNER};",
        *(f"ALTER TABLE assaf_os.{table} OWNER TO {OWNER};" for table in OWNED_TABLES),
        *(f"ALTER FUNCTION assaf_os.{function} OWNER TO {OWNER};" for function in OWNED_FUNCTIONS),
        f"REVOKE ALL ON SCHEMA assaf_os FROM {GRANTEES};",
        f"REVOKE ALL ON ALL TABLES IN SCHEMA assaf_os FROM {GRANTEES};",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA assaf_os FROM {GRANTEES};",
        f"REVOKE ALL ON ALL ROUTINES IN SCHEMA assaf_os FROM {GRANTEES};",
        # A --no-privileges restore also drops the owner's default ACL.
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;",
        *GRANTS,
    )


def validate(conn, *, main_role: str) -> None:
    """The stand-in for validate_ledger_catalog. Raises ValueError on the first wrong fact."""
    row = conn.execute(text(
        "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname='assaf_os'"
    )).scalar()
    if row != OWNER:
        raise ValueError("schema_owner")
    wrong_owner = conn.execute(text(
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='assaf_os' AND c.relkind IN ('r','p','v','m','f','S') "
        f"AND pg_get_userbyid(c.relowner) <> '{OWNER}'"
    )).scalar() + conn.execute(text(
        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
        f"WHERE n.nspname='assaf_os' AND pg_get_userbyid(p.proowner) <> '{OWNER}'"
    )).scalar()
    if wrong_owner:
        raise ValueError("object_owner")
    public_function = conn.execute(text(
        "SELECT count(*) FROM pg_proc p CROSS JOIN LATERAL aclexplode("
        "coalesce(p.proacl, acldefault('f', p.proowner))) a "
        "WHERE p.pronamespace='assaf_os'::regnamespace AND a.grantee=0 "
        "AND a.privilege_type='EXECUTE'"
    )).scalar()
    public_object = conn.execute(text(
        "SELECT count(*) FROM pg_class c CROSS JOIN LATERAL aclexplode("
        "coalesce(c.relacl, acldefault('r', c.relowner))) a "
        "WHERE c.relnamespace='assaf_os'::regnamespace AND c.relkind='r' AND a.grantee=0"
    )).scalar() + conn.execute(text(
        "SELECT count(*) FROM pg_namespace s CROSS JOIN LATERAL aclexplode("
        "coalesce(s.nspacl, acldefault('n', s.nspowner))) a "
        "WHERE s.nspname='assaf_os' AND a.grantee=0"
    )).scalar()
    if public_function or public_object:
        raise ValueError("public_grants")
    granted = set()
    for table in OWNED_TABLES:
        for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
            if conn.execute(text(
                "SELECT has_table_privilege(CAST(:role AS name), CAST(:name AS text), "
                "CAST(:priv AS text))"
            ), {"role": RUNTIME, "name": f"assaf_os.{table}", "priv": privilege}).scalar():
                granted.add(("table", table, privilege))
    for function in OWNED_FUNCTIONS:
        if conn.execute(text(
            "SELECT has_function_privilege(CAST(:role AS name), CAST(:name AS text), "
            "'EXECUTE')"
        ), {"role": RUNTIME, "name": f"assaf_os.{function}"}).scalar():
            granted.add(("function", function, "EXECUTE"))
    if conn.execute(text(
        "SELECT has_schema_privilege(CAST(:role AS name), 'assaf_os', 'USAGE')"
    ), {"role": RUNTIME}).scalar():
        granted.add(("schema", "assaf_os", "USAGE"))
    if granted != EXPECTED_LEDGER_PRIVILEGES:
        raise ValueError("ledger_grants")
    _validate_main_role_catalog(conn, main_role)


def _url(variable: str, role: str, port: int, database: str) -> URL:
    raw = os.environ.get(variable, "").strip()
    scheme, separator, rest = raw.partition("://")
    if separator and scheme.lower() in {"postgres", "postgresql"}:
        raw = "postgresql+psycopg://" + rest
    url = make_url(raw)
    binding = (url.drivername, url.host, url.port, url.database, url.username, dict(url.query))
    if binding != ("postgresql+psycopg", "127.0.0.1", port, database, role, {}):
        raise ValueError("ledger_target_unpinned")
    return url


def apply(mode: str) -> tuple[str, ...]:
    port = int(os.environ["V210_STANDIN_PORT"])
    database, migration_role, main_role = "mia", "mia", "mia_app"
    migration = create_engine(_url("MIA_OS_LEDGER_MIGRATION_DATABASE_URL", migration_role, port,
                                   database))
    main = create_engine(_url("MIA_DATABASE_URL", main_role, port, database))
    executed: list[str] = []
    try:
        with migration.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '30s'"))
            conn.execute(text("SET LOCAL lock_timeout = '5s'"))
            if conn.scalar(text("SELECT to_regnamespace('assaf_os') IS NULL")):
                return (NOOP,)
            with main.connect() as app:
                identity = app.execute(text("SELECT session_user, current_user")).one()
            if tuple(identity) != (main_role, main_role):
                raise ValueError("ledger_target_unpinned")
            for statement in statements(database, migration_role):
                created = re.match(r"CREATE ROLE (\w+) ", statement)
                if created and conn.scalar(text("SELECT to_regrole(CAST(:r AS text)) IS NOT NULL"),
                                           {"r": created.group(1)}):
                    continue  # adopt it; keep its password
                conn.exec_driver_sql(statement)
                executed.append(statement)
                if created and created.group(1) == RUNTIME and (
                    os.environ.get("V210_ETC_MIA_READONLY") == "1"
                ):
                    # The real script stages the new role's DSN in /etc/mia/runtime.env before
                    # commit. On a read-only mount that write fails and the transaction rolls back.
                    raise ValueError("runtime_env_not_writable")
            validate(conn, main_role=main_role)
            if os.environ.get("V210_STANDIN_FAIL") == "1":
                raise ValueError("forced_refusal")
        return tuple(executed)
    finally:
        migration.dispose()
        main.dispose()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, choices=("mia-vm",))
    parser.add_argument("--mode", choices=("provision", "reapply"), default="provision")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-sha")
    args = parser.parse_args(argv)
    if args.mode != "reapply" or not args.apply:
        parser.exit(2, "the stand-in only implements --mode reapply --apply\n")
    sha = os.environ.get("MIA_BUILD_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or args.approved_sha != sha:
        parser.exit(1, "ledger_owner_go_sha_required\n")
    try:
        executed = apply(args.mode)
    except Exception:
        parser.exit(1, "ledger_provision_refused\n")
    print("\n".join(executed))


if __name__ == "__main__":
    main()
