"""VM-local, owner-operated staging/cutover. Standard library only; no secret output.

Modes: stage (default), --check, --activate, --enforce, --rollback, and --reapply, the
post-restore repair that `mia restore` runs (V2-10) after pg_restore and before the services
start. Only --activate, --enforce and --rollback write /etc/mia/runtime.env; --reapply and
--check never write any env file.
"""

import argparse
import json
import os
import re
import secrets
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

SQL_PATH = Path(__file__).with_name("sql") / "app-role-demote.sql"
ROLE_EXISTS_SQL = "SELECT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mia_app')"
# Fixed reason codes: the ones the reviewed SQL raises, and the ones this tool raises. Only these
# are ever printed. Everything else (psql stderr can carry statements, DSNs and role names, and
# arbitrary exceptions can carry anything) collapses to a generic code.
SQL_REASONS = frozenset(
    {
        "app_role_admin_required",
        "app_role_target_invalid",
        "app_role_reapply_role_missing",
        "app_role_existing_unsafe",
        "app_role_database_owner_unsafe",
        "app_role_membership_unsafe",
        "app_role_ledger_access_unsafe",
        "app_role_extension_object_requires_review",
    }
)
TOOL_REASONS = frozenset(
    {
        "app_role_auth_failed",
        "app_role_check_failed",
        "app_role_database_failed",
        "app_role_dsn_invalid",
        "app_role_dsn_mismatch",
        "app_role_file_unsafe",
        "app_role_mode_invalid",
        "app_role_root_required",
        "app_role_staged_password_invalid",
        "app_role_unmanaged",
    }
)
SQL_ERROR_LINE = re.compile(r"^ERROR:\s+(app_role_[a-z_]+)\s*$", re.MULTILINE)
CHECK_SQL = """
SELECT json_build_object(
 'role_exists', EXISTS(SELECT 1 FROM pg_roles WHERE rolname='mia_app'),
 'role_safe', coalesce((SELECT NOT (rolsuper OR rolcreaterole OR rolcreatedb
    OR rolreplication OR rolbypassrls OR rolinherit OR NOT rolcanlogin)
    FROM pg_roles WHERE rolname='mia_app'), false),
 'no_membership', NOT EXISTS(SELECT 1 FROM pg_auth_members WHERE member=to_regrole('mia_app')),
 'not_database_owner', NOT EXISTS(SELECT 1 FROM pg_database WHERE datdba=to_regrole('mia_app')),
 'public_owned', NOT EXISTS(SELECT 1 FROM pg_class
    WHERE relnamespace='public'::regnamespace AND relkind IN ('r','p','v','m','f','S')
    AND relowner IS DISTINCT FROM to_regrole('mia_app'))
    AND NOT EXISTS(SELECT 1 FROM pg_proc WHERE pronamespace='public'::regnamespace
    AND proowner IS DISTINCT FROM to_regrole('mia_app')),
 'public_create', coalesce(has_schema_privilege(to_regrole('mia_app'),'public','USAGE')
    AND has_schema_privilege(to_regrole('mia_app'),'public','CREATE'),false),
 'database_create', coalesce(has_database_privilege(to_regrole('mia_app'),
    current_database(),'CREATE'),false),
 'ledger_access', EXISTS(SELECT 1 FROM pg_namespace WHERE nspname='assaf_os' AND
    has_schema_privilege(to_regrole('mia_app'),oid,'USAGE,CREATE')) OR EXISTS(
    SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='assaf_os' AND CASE
    WHEN c.relkind='S' THEN
    has_sequence_privilege(to_regrole('mia_app'),c.oid,'USAGE,SELECT,UPDATE')
    WHEN c.relkind IN ('r','p','v','m','f') THEN
    has_table_privilege(to_regrole('mia_app'),c.oid,
       'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER') OR
    has_any_column_privilege(to_regrole('mia_app'),c.oid,'SELECT,INSERT,UPDATE,REFERENCES')
    ELSE false END) OR EXISTS(
    SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='assaf_os' AND has_function_privilege(to_regrole('mia_app'),p.oid,'EXECUTE'))
)
"""


def database(sql: str) -> str:
    # The postgres service is already running. Socket auth as the original mia admin.
    # SQL/credentials go over stdin, never argv; suppress all provider output on error.
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "mia",
            "-f",
            "/opt/mia/compose.yaml",
            "--env-file",
            "/opt/mia/tag.env",
            "exec",
            "-T",
            "postgres",
            "psql",
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "mia",
            "-d",
            "mia",
        ],
        input=sql,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    if result.returncode:
        raise ValueError(sql_reason(result.stderr))
    return result.stdout.strip()


def sql_reason(stderr: str | None) -> str:
    """The fixed reason code the reviewed SQL raised, else a generic code. Nothing else leaks."""
    for code in SQL_ERROR_LINE.findall(stderr or ""):
        if code in SQL_REASONS:
            return code
    return "app_role_database_failed"


def authenticate(password: str) -> None:
    # Test the staged credential over TCP (the runtime path), not admin socket trust.
    # Password enters the child environment from stdin; it is never a docker argv value.
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            "mia",
            "-f",
            "/opt/mia/compose.yaml",
            "--env-file",
            "/opt/mia/tag.env",
            "exec",
            "-T",
            "postgres",
            "sh",
            "-c",
            "IFS= read -r PGPASSWORD; export PGPASSWORD; "
            "exec psql -XqAt -v ON_ERROR_STOP=1 -h 127.0.0.1 -U mia_app -d mia "
            "-c \"SELECT session_user = current_user AND current_user = 'mia_app'\"",
        ],
        input=password + "\n",
        text=True,
        capture_output=True,
        check=False,
        timeout=20,
    )
    if result.returncode or result.stdout.strip() != "t":
        raise ValueError("app_role_auth_failed")


def require_safe(execute, *, allow_ledger_access: bool = False) -> dict:
    state = json.loads(execute(CHECK_SQL))
    required = (
        "role_exists",
        "role_safe",
        "no_membership",
        "not_database_owner",
        "public_owned",
        "public_create",
    )
    ledger_ok = (False, True) if allow_ledger_access else (False,)
    if (
        not all(state.get(key) is True for key in required)
        or state.get("database_create") is not False
        or state.get("ledger_access") not in ledger_ok
    ):
        raise ValueError("app_role_check_failed")
    return state


def atomic_write(path: Path, content: str) -> None:
    if path.is_symlink():
        raise ValueError("app_role_file_unsafe")
    if path.exists() and path.read_text() == content:
        return
    fd, name = tempfile.mkstemp(prefix=".app-role-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def require_private_metadata(info: os.stat_result) -> None:
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise ValueError("app_role_file_unsafe")


def read_env(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("app_role_file_unsafe")
    if os.name == "posix":
        require_private_metadata(path.stat())
    return path.read_text(encoding="utf-8")


def dsn(content: str, user: str) -> str:
    values = re.findall(r"^MIA_DATABASE_URL=(.+)$", content, re.MULTILINE)
    if len(values) != 1:
        raise ValueError("app_role_dsn_invalid")
    value = values[0]
    parsed = urlsplit(value)
    if (
        parsed.scheme != "postgresql"
        or parsed.username != user
        or parsed.hostname != "127.0.0.1"
        or parsed.port != 5432
        or parsed.path != "/mia"
        or not parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("app_role_dsn_invalid")
    return value


def update_env(content: str, **updates: str) -> str:
    lines = [line for line in content.splitlines() if line.split("=", 1)[0] not in updates]
    lines.extend(f"{key}={value}" for key, value in updates.items())
    return "\n".join(lines) + "\n"


def transaction(credential_sql: str = "", *, repair: bool = False) -> str:
    """The one reviewed demote script in one transaction, with statement logging suppressed."""
    return (
        "BEGIN; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='90s';\n"
        "SET LOCAL log_statement='none'; SET LOCAL log_min_error_statement='panic';\n"
        "SET LOCAL log_min_duration_statement=-1; SET LOCAL log_min_duration_sample=-1;\n"
        "SET LOCAL log_duration=off;\n"
        + ("SET LOCAL mia.app_role_reapply='on';\n" if repair else "")
        + SQL_PATH.read_text(encoding="utf-8")
        + "\n"
        + credential_sql
        + "COMMIT;\n"
    )


def reapply(execute) -> dict[str, bool]:
    """Post-restore repair, run as superuser mia under the caller's state lock.

    `mia restore` drops and recreates the database, then runs `pg_restore --no-owner
    --no-privileges`: every object is owned by mia, and every grant, default ACL and the
    schema CREATE grant are gone. The role itself is cluster-global and survives. This re-runs
    the idempotent ownership transfer and grants. It never reads or writes an env file, never
    creates a role or rotates a password, and never restarts anything. Before the demote
    exists there is nothing to repair, so that case is a no-op, not a failed restore.
    """
    if execute(ROLE_EXISTS_SQL) != "t":
        return {"reapplied": False, "role_exists": False}
    execute(transaction(repair=True))
    # Residual assaf_os access is reported, not fatal: a restore resurrects PUBLIC EXECUTE on
    # the ledger functions, and V2-15's reapply clears it. V2-10 then runs the real validator.
    state = require_safe(execute, allow_ledger_access=True)
    return {"reapplied": True, "role_exists": True, "ledger_access": state["ledger_access"]}


def perform(mode: str, directory: Path, execute=database) -> dict[str, bool]:
    if mode == "reapply":
        return reapply(execute)
    runtime = directory / "runtime.env"
    staged = directory / "app-role.staged.env"
    rollback = directory / "app-role.rollback.env"
    current = read_env(runtime)
    if mode == "check":
        result = json.loads(execute(CHECK_SQL))
        if not isinstance(result, dict) or not all(type(v) is bool for v in result.values()):
            raise ValueError("app_role_check_failed")
        result["active_is_app"] = "MIA_DATABASE_URL=postgresql://mia_app:" in current
        result["enforce"] = "MIA_APP_ROLE_POSTURE=enforce" in current.splitlines()
        return result
    if mode == "rollback":
        old = dsn(read_env(rollback), "mia")
        atomic_write(
            runtime, update_env(current, MIA_DATABASE_URL=old, MIA_APP_ROLE_POSTURE="observe")
        )
        return {"rollback_staged": True, "restart_required": True}
    if mode in {"activate", "enforce"}:
        new = dsn(read_env(staged), "mia_app")
        dsn(read_env(rollback), "mia")
        require_safe(execute)
        authenticate(urlsplit(new).password)
        if mode == "enforce":
            if dsn(current, "mia_app") != new:
                raise ValueError("app_role_dsn_mismatch")
        atomic_write(
            runtime,
            update_env(
                current,
                MIA_DATABASE_URL=new,
                MIA_APP_ROLE_POSTURE=("enforce" if mode == "enforce" else "observe"),
            ),
        )
        return {"runtime_updated": True, "restart_required": True}
    if mode != "stage":
        raise ValueError("app_role_mode_invalid")
    exists = execute(ROLE_EXISTS_SQL) == "t"
    if exists and not staged.exists():
        raise ValueError("app_role_unmanaged")
    if not rollback.exists():
        old = dsn(current, "mia")
        atomic_write(rollback, f"MIA_DATABASE_URL={old}\n")
    else:
        dsn(read_env(rollback), "mia")
    if not staged.exists():
        password = secrets.token_hex(32)
        atomic_write(
            staged, f"MIA_DATABASE_URL=postgresql://mia_app:{password}@127.0.0.1:5432/mia\n"
        )
    new = dsn(read_env(staged), "mia_app")
    password = urlsplit(new).password
    if not re.fullmatch(r"[a-f0-9]{64}", password or ""):
        raise ValueError("app_role_staged_password_invalid")
    # A failed transaction leaves the same staged credential for a safe retry.
    # Existing managed roles are never rotated on re-run.
    credential_sql = "" if exists else f"ALTER ROLE mia_app PASSWORD '{password}';\n"
    execute(transaction(credential_sql))
    authenticate(password)
    return {"provisioned": True, "dsn_staged": True, "runtime_changed": False}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    for mode in ("check", "activate", "enforce", "rollback", "reapply"):
        modes.add_argument(f"--{mode}", action="store_const", const=mode, dest="mode")
    args = parser.parse_args()
    try:
        if os.geteuid() != 0:
            raise ValueError("app_role_root_required")
        result = perform(args.mode or "stage", Path("/etc/mia"))
    except Exception as failure:
        # Never stringify exceptions, SQL, subprocess output, paths, or credentials. Only an
        # exact fixed reason code (this tool's own, or one the reviewed SQL raised) is printed.
        reason = str(failure) if isinstance(failure, ValueError) else ""
        known = reason in SQL_REASONS or reason in TOOL_REASONS
        print(f"reason={reason if known else 'app_role_operation_failed'} ok=false")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
