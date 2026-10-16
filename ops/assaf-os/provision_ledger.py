"""Pinned ledger role provisioning: print the exact plan (default) or apply it.

Run from the repo:
    uv run --offline python ops/assaf-os/provision_ledger.py --target local-v2-15 [--mode reapply]

--mode provision (the default) creates the runtime and retention roles and refuses
existing ones. --mode reapply is the restore post-step (V2_PG_CONTRACTS.md §18.1): it
creates only missing roles, never changes an existing role's password, re-owns
assaf_os, re-applies every REVOKE and GRANT, and validates. On a database without
assaf_os it is a no-op. --apply needs --approved-sha equal to MIA_BUILD_SHA and the
pinned MIA_OS_LEDGER_MIGRATION_DATABASE_URL and MIA_DATABASE_URL. For a target with a
runtime env file (mia-vm), a newly created runtime role gets a generated password whose
DSN is written to that VM-owned file as MIA_OWNER_OS_LEDGER_DATABASE_URL before commit.
Nothing prints a DSN or a password.
"""

import argparse
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, Connection, Engine, make_url
from sqlalchemy.orm import Session

# Allow direct execution without installing another console entrypoint.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db.os_ledger_migrate import OWNER_ROLE, RUNTIME_DSN_ENV, RUNTIME_ROLE  # noqa: E402
from app.services.os_ledger.role_policy import (  # noqa: E402
    OWNED_FUNCTIONS,
    OWNED_TABLES,
    RETENTION_ROLE,
    ledger_grant_statements,
    validate_ledger_catalog,
    validate_ledger_role,
)


class Target(NamedTuple):
    host: str
    port: int
    database: str
    migration_role: str
    main_role: str
    runtime_env: str | None = None


# Every field is an exact pin. Adding or changing a target is a reviewed code change.
_TARGETS = {
    "local-v2-15": Target("127.0.0.1", 56215, "mia_ledger_test", "postgres", "v215_main"),
    # The VM (ops/gcp/vm): loopback PostgreSQL 16, database mia, the superuser
    # migration identity mia, V2-14a's demoted main role mia_app, and the VM-owned
    # runtime env file shared by the migrate and app services (FORK-LEDGER-DSN).
    "mia-vm": Target("127.0.0.1", 5432, "mia", "mia", "mia_app", "/etc/mia/runtime.env"),
}
MODES = ("provision", "reapply")
NOOP = "-- assaf_os is absent: reapply is a no-op"
_POSTURE = "NOINHERIT NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS"
_ROLE_PLAN = (
    (OWNER_ROLE, f"CREATE ROLE {OWNER_ROLE} NOLOGIN {_POSTURE};"),
    (RUNTIME_ROLE, f"CREATE ROLE {RUNTIME_ROLE} LOGIN {_POSTURE};"),
    (RETENTION_ROLE, f"CREATE ROLE {RETENTION_ROLE} NOLOGIN {_POSTURE};"),
)
_REAPPLY_NOTE = (
    "-- reapply: each CREATE ROLE runs only for a missing role; "
    "existing roles and passwords are kept"
)
_PASSWORD_NOTE = (
    f"-- {RUNTIME_ROLE}: password set from a SCRAM verifier; "
    f"its DSN is staged as {RUNTIME_DSN_ENV} in the pinned runtime env file"
)
_GRANTEES = f"PUBLIC, {RUNTIME_ROLE}, {RETENTION_ROLE}"
_VERIFIER = re.compile(r"SCRAM-SHA-256\$\d+:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+")


def provision_statements(name: str, mode: str = "provision") -> tuple[str, ...]:
    """Exact plan for a pinned target; ValueError('ledger_target_unpinned') otherwise."""
    if name not in _TARGETS or mode not in MODES:
        raise ValueError("ledger_target_unpinned")
    target = _TARGETS[name]
    return (
        *([_REAPPLY_NOTE] if mode == "reapply" else []),
        # provision never creates the owner: V2-14's migration did, with the schema.
        *(statement for role, statement in _ROLE_PLAN if mode == "reapply" or role != OWNER_ROLE),
        # PostgreSQL grants TEMP through PUBLIC by default and a per-role revoke cannot
        # cancel it. Neither the runtime nor the main role may hold TEMP.
        f"REVOKE TEMPORARY ON DATABASE {target.database} FROM PUBLIC;",
        f"REVOKE ALL ON DATABASE {target.database} FROM {RUNTIME_ROLE}, {RETENTION_ROLE};",
        f"GRANT CONNECT ON DATABASE {target.database} TO {RUNTIME_ROLE};",
        # §5.5: the runtime guard derives the migration identity from this one edge.
        f"GRANT {OWNER_ROLE} TO {target.migration_role} "
        "WITH ADMIN TRUE, SET FALSE, INHERIT FALSE;",
        # A --no-owner restore leaves everything owned by the restoring superuser.
        f"ALTER SCHEMA assaf_os OWNER TO {OWNER_ROLE};",
        *(f"ALTER TABLE assaf_os.{table} OWNER TO {OWNER_ROLE};" for table in OWNED_TABLES),
        *(f"ALTER FUNCTION assaf_os.{function} OWNER TO {OWNER_ROLE};"
          for function in OWNED_FUNCTIONS),
        f"REVOKE ALL ON SCHEMA assaf_os FROM {_GRANTEES};",
        f"REVOKE ALL ON ALL TABLES IN SCHEMA assaf_os FROM {_GRANTEES};",
        f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA assaf_os FROM {_GRANTEES};",
        f"REVOKE ALL ON ALL ROUTINES IN SCHEMA assaf_os FROM {_GRANTEES};",
        # A --no-privileges restore also drops the owner's global default ACL.
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {OWNER_ROLE} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC;",
        *ledger_grant_statements(),
    )


def _ledger_dsn(target: Target, password: str) -> str:
    return (
        f"postgresql://{RUNTIME_ROLE}:{password}@{target.host}:{target.port}/{target.database}"
    )


def _read_private(path: Path) -> str:
    # VM-owned: a regular file (no symlink) owned by this process's user (root on the
    # VM) and unreadable by group and others.
    if path.is_symlink() or not path.is_file():
        raise ValueError("ledger_runtime_env_invalid")
    if os.name == "posix":
        info = path.stat()
        if info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError("ledger_runtime_env_invalid")
    return path.read_bytes().decode("utf-8")


def _atomic_write(path: Path, content: str) -> None:
    fd, name = tempfile.mkstemp(prefix=".ledger-", dir=path.parent)
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


def runtime_credential(target: Target, *, create: bool = True) -> str | None:
    """The runtime role's password as staged in the target's VM-owned env file.

    A staged DSN is reused as is, so a retry never rotates it. It must be the single,
    canonical MIA_OWNER_OS_LEDGER_DATABASE_URL line for this exact pin, else
    ValueError('ledger_runtime_env_invalid'). With create=True and nothing staged, a
    new 256-bit password is generated and its DSN appended atomically (mode 0600),
    keeping every other line. Returns None for a target without an env file, or when
    nothing is staged and create=False. Never prints either value.
    """
    if target.runtime_env is None:
        return None
    path = Path(target.runtime_env)
    lines = _read_private(path).splitlines()
    staged = [line for line in lines if RUNTIME_DSN_ENV in line]
    canonical = re.compile(
        re.escape(f"{RUNTIME_DSN_ENV}=postgresql://{RUNTIME_ROLE}:") + r"([0-9a-f]{64})"
        + re.escape(f"@{target.host}:{target.port}/{target.database}")
    )
    if staged:
        match = canonical.fullmatch(staged[0])
        if len(staged) != 1 or not match:
            raise ValueError("ledger_runtime_env_invalid")
        return match.group(1)
    if not create:
        return None
    password = secrets.token_hex(32)
    _atomic_write(path, "".join(f"{line}\n" for line in lines)
                  + f"{RUNTIME_DSN_ENV}={_ledger_dsn(target, password)}\n")
    return password


def _pinned_url(target: Target, variable: str, role: str) -> URL:
    raw = os.environ.get(variable, "").strip()
    scheme, separator, rest = raw.partition("://")
    if separator and scheme.lower() in {"postgres", "postgresql"}:
        raw = "postgresql+psycopg://" + rest  # runtime.env DSNs use the bare scheme
    url = make_url(raw)
    binding = (url.drivername, url.host, url.port, url.database, url.username, dict(url.query))
    if binding != ("postgresql+psycopg", target.host, target.port, target.database, role, {}):
        raise ValueError("ledger_target_unpinned")
    return url


def _verifier(conn: Connection, password: str) -> str:
    # libpq computes the SCRAM-SHA-256 verifier client-side, so the plaintext never
    # reaches the server, its statement log or pg_stat_activity.
    pgconn = conn.connection.dbapi_connection.pgconn
    verifier = pgconn.encrypt_password(
        password.encode(), RUNTIME_ROLE.encode(), b"scram-sha-256"
    ).decode("ascii")
    if not _VERIFIER.fullmatch(verifier):
        raise ValueError("ledger_provision_refused")
    return verifier


def _verify_runtime(target: Target, url: URL, main: Engine, password: str | None) -> None:
    # After commit: the runtime DSN really authenticates, as itself, on the same server
    # and database, and the full §5.5 hook accepts it with the pinned migration role.
    ledger = create_engine(
        url.set(username=RUNTIME_ROLE, password=password), connect_args={"connect_timeout": 10},
    )
    try:
        with Session(ledger) as db:
            validate_ledger_role(db, main, migration_role=target.migration_role)
    finally:
        ledger.dispose()


def _main_identity(main: Engine, conn: Connection, target: Target) -> str:
    # The real main DSN: unimpersonated, the pinned role, distinct from the migration
    # identity, on the same server and database. Its demotion posture is checked inside
    # the transaction (validate_ledger_catalog runs V2-14's _validate_main_role_catalog
    # on it), so a restore's PUBLIC EXECUTE that this plan revokes cannot block reapply.
    identity = text(
        "SELECT current_database(), inet_server_addr()::text, inet_server_port(), "
        "session_user, current_user"
    )
    with main.connect() as app:
        theirs = app.execute(identity).one()
    ours = conn.execute(identity).one()
    if (theirs[:3] != ours[:3] or None in theirs[:3] or theirs[3] != theirs[4]
            or theirs[3] != target.main_role or theirs[3] == ours[3]):
        raise ValueError("ledger_target_unpinned")
    return theirs[3]


def _apply(target: Target, mode: str, statements: tuple[str, ...]) -> tuple[str, ...]:
    engines: list[Engine] = []
    try:
        url = _pinned_url(target, "MIA_OS_LEDGER_MIGRATION_DATABASE_URL", target.migration_role)
        migration = create_engine(url, connect_args={"connect_timeout": 10})
        engines.append(migration)
        password = None
        executed: list[str] = []
        with migration.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '30s'"))
            conn.execute(text("SET LOCAL lock_timeout = '5s'"))
            conn.execute(text("SET LOCAL search_path = pg_catalog, pg_temp"))
            conn.execute(text("SELECT pg_advisory_xact_lock(734927, 14)"))
            identity = conn.execute(text(
                "SELECT current_database(),session_user,current_user"
            )).one()
            if tuple(identity) != (target.database, target.migration_role, target.migration_role):
                raise ValueError("ledger_target_unpinned")
            absent = conn.scalar(text("SELECT to_regnamespace('assaf_os') IS NULL"))
            if mode == "reapply" and absent:
                return (NOOP,)  # nothing to restore; the main DSN is not even needed
            main = create_engine(
                _pinned_url(target, "MIA_DATABASE_URL", target.main_role),
                connect_args={"connect_timeout": 10},
            )
            engines.append(main)
            actual_main = _main_identity(main, conn, target)
            for statement in statements:
                if statement.startswith("--"):
                    continue
                created = re.match(r"CREATE ROLE (\w+) ", statement)
                role = created.group(1) if created else None
                if role and mode == "reapply" and conn.scalar(
                    text("SELECT to_regrole(:role) IS NOT NULL"), {"role": role}
                ):
                    continue  # adopt it; the catalog audit below decides
                # provision: CREATE ROLE refuses an existing role; never adopt one.
                conn.exec_driver_sql(statement)
                executed.append(statement)
                if role == RUNTIME_ROLE and target.runtime_env:
                    # Staged in the env file BEFORE commit, so a failed transaction
                    # retries with the same credential instead of losing it.
                    password = runtime_credential(target)
                    conn.exec_driver_sql(
                        f"ALTER ROLE {RUNTIME_ROLE} PASSWORD '{_verifier(conn, password)}'"
                    )
                    executed.append(_PASSWORD_NOTE)
            validate_ledger_catalog(
                conn, main_role=actual_main, migration_role=target.migration_role,
            )
        if password is None:
            password = runtime_credential(target, create=False)
        _verify_runtime(target, url, main, password)
        return tuple(executed)
    finally:
        for engine in engines:
            engine.dispose()


def main(argv: list[str] | None = None) -> None:
    """Dry-run CLI. SystemExit 2 on arguments; 1 with a sanitized refusal code on apply."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--target", required=True, choices=tuple(_TARGETS))
    parser.add_argument("--mode", choices=MODES, default="provision")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-sha")
    args = parser.parse_args(argv)
    statements = provision_statements(args.target, args.mode)
    if not args.apply:
        print("\n".join(statements))
        return
    sha = os.environ.get("MIA_BUILD_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or args.approved_sha != sha:
        parser.exit(1, "ledger_owner_go_sha_required\n")
    try:
        executed = _apply(_TARGETS[args.target], args.mode, statements)
    except Exception:
        # Never print DSNs, passwords, driver exceptions or provider/catalog payloads.
        parser.exit(1, "ledger_provision_refused\n")
    print("\n".join(executed))


if __name__ == "__main__":
    main()
