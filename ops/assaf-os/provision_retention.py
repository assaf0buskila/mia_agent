"""Provision the os-retention job's database credential (V2_PG_CONTRACTS.md section 22 E8).

Run from the repo (dry run, the default, prints the plan and touches nothing):
    uv run --offline python ops/assaf-os/provision_retention.py --target mia-vm

``assaf_os_retention`` is LOGIN NOINHERIT with exactly its section 18.2 grants (V2-14b), but it has
no password: V2-15's provisioning stages only the runtime role's. This is the same step for the
retention role. ``--apply`` needs ``--approved-sha`` equal to MIA_BUILD_SHA and the pinned
MIA_OS_LEDGER_MIGRATION_DATABASE_URL and MIA_DATABASE_URL (the environment of the
``os-ledger-migrate`` service). It

- refuses a retention role that is missing, or is not LOGIN NOINHERIT with no other power;
- refuses, with ``retention_connect_blocked``, when PUBLIC no longer holds CONNECT on the database.
  The retention role has no database ACL entry of its own (``validate_ledger_catalog`` pins that),
  so it reaches the database through PUBLIC. Revoking PUBLIC CONNECT needs the section 18.2 erratum
  of E8.2 first, which grants exactly CONNECT to the retention role and teaches the validator;
- runs the section 18 catalog validator before it changes anything;
- generates a 256-bit password once and stages the DSN as MIA_OS_LEDGER_RETENTION_DATABASE_URL in
  the job-only env file (VM-owned, mode 0600, this one key, never in ``mia-env``, never in
  ``runtime.env``) BEFORE the transaction commits, so a retry reuses it instead of rotating it;
- sets the password from a SCRAM-SHA-256 verifier that libpq computes on the client, so the
  plaintext never reaches the server, its statement log or pg_stat_activity; and
- after commit connects as the role through the staged DSN and checks who it is.

To rotate the credential, remove the env file and run this again. Nothing prints a DSN, a password
or a driver message: a refusal is one fixed reason code on stderr and exit status 1.
"""

import argparse
import os
import re
import secrets
import sys
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, make_url

# Allow direct execution, and the sibling import of provision_ledger's pinned helpers.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import provision_ledger as ledger  # noqa: E402
from app.services.os_ledger.role_policy import (  # noqa: E402
    RETENTION_ROLE,
    validate_ledger_catalog,
)

DSN_ENV = "MIA_OS_LEDGER_RETENTION_DATABASE_URL"
CODES = frozenset({
    "retention_target_unpinned", "retention_role_missing", "retention_role_unsafe",
    "retention_connect_blocked", "retention_env_invalid", "retention_catalog_refused",
    "retention_login_failed", "retention_go_sha_required",
})
_REFUSED = "retention_provision_refused"


class RetentionTarget(NamedTuple):
    host: str
    port: int
    database: str
    migration_role: str
    main_role: str
    retention_env: str


# Every field is an exact pin. Adding or changing a target is a reviewed code change.
_TARGETS = {
    "mia-vm": RetentionTarget(
        "127.0.0.1", 5432, "mia", "mia", "mia_app", "/etc/mia/os-retention.env"
    ),
}
_PLAN = (
    f"-- {RETENTION_ROLE}: must exist as LOGIN NOINHERIT NOSUPERUSER NOCREATEROLE NOCREATEDB "
    "NOREPLICATION NOBYPASSRLS, and PUBLIC must hold CONNECT on the database",
    "-- the section 18 catalog validator runs first and must pass",
    f"-- {RETENTION_ROLE}: password set from a SCRAM verifier; its DSN is staged as {DSN_ENV}",
    "-- in the job-only env file (VM-owned, 0600, this one key, never in mia-env)",
    f"ALTER ROLE {RETENTION_ROLE} PASSWORD '<SCRAM-SHA-256 verifier computed by libpq>';",
)
_POSTURE = (True, False, False, False, False, False, False)


def plan(name: str) -> tuple[str, ...]:
    """The exact plan for a pinned target; ValueError('retention_target_unpinned') otherwise."""
    if name not in _TARGETS:
        raise ValueError("retention_target_unpinned")
    target = _TARGETS[name]
    return (*_PLAN, f"-- env file: {target.retention_env}")


def _dsn(target: RetentionTarget, password: str) -> str:
    return (
        f"postgresql://{RETENTION_ROLE}:{password}@{target.host}:{target.port}/{target.database}"
    )


def retention_credential(target: RetentionTarget, *, create: bool = True) -> str | None:
    """The retention password as staged in the job-only env file.

    The file holds exactly one line, the canonical DSN for this pin, else
    ValueError('retention_env_invalid'). A staged DSN is reused as is, so a retry never rotates
    it. With create=True and nothing staged, a new 256-bit password is generated and its DSN
    written atomically (mode 0600). Returns None when nothing is staged and create=False.
    Never prints either value.
    """
    path = Path(target.retention_env)
    if path.is_symlink():
        raise ValueError("retention_env_invalid")
    if path.exists():
        try:
            lines = [line for line in ledger._read_private(path).splitlines() if line.strip()]
        except ValueError:
            raise ValueError("retention_env_invalid") from None
        canonical = re.compile(
            re.escape(f"{DSN_ENV}=postgresql://{RETENTION_ROLE}:") + r"([0-9a-f]{64})"
            + re.escape(f"@{target.host}:{target.port}/{target.database}")
        )
        match = canonical.fullmatch(lines[0]) if len(lines) == 1 else None
        if match is None:
            raise ValueError("retention_env_invalid")
        return match.group(1)
    if not create:
        return None
    password = secrets.token_hex(32)
    ledger._atomic_write(path, f"{DSN_ENV}={_dsn(target, password)}\n")
    return password


def _verifier(conn: Connection, password: str) -> str:
    # libpq computes the SCRAM-SHA-256 verifier client-side.
    pgconn = conn.connection.dbapi_connection.pgconn
    verifier = pgconn.encrypt_password(
        password.encode(), RETENTION_ROLE.encode(), b"scram-sha-256"
    ).decode("ascii")
    if not ledger._VERIFIER.fullmatch(verifier):
        raise ValueError(_REFUSED)
    return verifier


def _check_role(conn: Connection) -> None:
    row = conn.execute(text(
        "SELECT rolcanlogin, rolinherit, rolsuper, rolcreaterole, rolcreatedb, rolreplication, "
        "rolbypassrls FROM pg_roles WHERE rolname = :role"
    ), {"role": RETENTION_ROLE}).first()
    if row is None:
        raise ValueError("retention_role_missing")
    if tuple(row) != _POSTURE:
        raise ValueError("retention_role_unsafe")


def _public_connect(conn: Connection) -> bool:
    return bool(conn.scalar(text(
        "SELECT EXISTS (SELECT 1 FROM pg_database d CROSS JOIN LATERAL aclexplode("
        "COALESCE(d.datacl, acldefault('d', d.datdba))) a WHERE d.datname = current_database() "
        "AND a.grantee = 0 AND a.privilege_type = 'CONNECT')"
    )))


def _verify_login(target: RetentionTarget, password: str) -> None:
    # After commit: the staged DSN really connects, as the retention role itself.
    url = make_url(f"postgresql+psycopg://{target.host}:{target.port}/{target.database}")
    engine = create_engine(
        url.set(username=RETENTION_ROLE, password=password), connect_args={"connect_timeout": 10},
    )
    try:
        with engine.connect() as conn:
            identity = conn.execute(text("SELECT session_user::text, current_user::text")).one()
            if tuple(identity) != (RETENTION_ROLE, RETENTION_ROLE):
                raise ValueError("retention_login_failed")
    except ValueError:
        raise
    except Exception:
        raise ValueError("retention_login_failed") from None
    finally:
        engine.dispose()


def apply(target: RetentionTarget) -> tuple[str, ...]:
    engines = []
    try:
        url = ledger._pinned_url(
            _as_ledger_target(target), "MIA_OS_LEDGER_MIGRATION_DATABASE_URL",
            target.migration_role,
        )
        migration = create_engine(url, connect_args={"connect_timeout": 10})
        engines.append(migration)
        with migration.begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '30s'"))
            conn.execute(text("SET LOCAL lock_timeout = '5s'"))
            conn.execute(text("SET LOCAL search_path = pg_catalog, pg_temp"))
            conn.execute(text("SELECT pg_advisory_xact_lock(734927, 31)"))
            identity = conn.execute(text(
                "SELECT current_database(), session_user, current_user"
            )).one()
            if tuple(identity) != (target.database, target.migration_role, target.migration_role):
                raise ValueError("retention_target_unpinned")
            _check_role(conn)
            if not _public_connect(conn):
                raise ValueError("retention_connect_blocked")
            main = create_engine(
                ledger._pinned_url(
                    _as_ledger_target(target), "MIA_DATABASE_URL", target.main_role
                ),
                connect_args={"connect_timeout": 10},
            )
            engines.append(main)
            actual_main = ledger._main_identity(main, conn, _as_ledger_target(target))
            try:
                validate_ledger_catalog(
                    conn, main_role=actual_main, migration_role=target.migration_role,
                )
            except ValueError:
                raise ValueError("retention_catalog_refused") from None
            # Staged BEFORE commit, so a failed transaction retries with the same credential.
            password = retention_credential(target, create=True)
            verifier = _verifier(conn, password)
            conn.exec_driver_sql(f"ALTER ROLE {RETENTION_ROLE} PASSWORD '{verifier}'")
        _verify_login(target, password)
        return (_PLAN[2], _PLAN[3], f"-- env file: {target.retention_env}")
    finally:
        for engine in engines:
            engine.dispose()


def _as_ledger_target(target: RetentionTarget) -> ledger.Target:
    """provision_ledger's pin helpers take its own Target; the extra field is unused by them."""
    return ledger.Target(
        target.host, target.port, target.database, target.migration_role, target.main_role,
        target.retention_env,
    )


def main(argv: list[str] | None = None) -> None:
    """Dry-run CLI. SystemExit 2 on arguments; 1 with a sanitized refusal code on apply."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--target", required=True, choices=tuple(_TARGETS))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-sha")
    args = parser.parse_args(argv)
    statements = plan(args.target)
    if not args.apply:
        print("\n".join(statements))
        return
    sha = os.environ.get("MIA_BUILD_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or args.approved_sha != sha:
        parser.exit(1, "retention_go_sha_required\n")
    try:
        executed = apply(_TARGETS[args.target])
    except ValueError as error:
        # Only a fixed reason code is ever printed: never a DSN, a password or a driver message.
        code = {"ledger_target_unpinned": "retention_target_unpinned"}.get(str(error), str(error))
        parser.exit(1, f"{code if code in CODES else _REFUSED}\n")
    except Exception:
        parser.exit(1, f"{_REFUSED}\n")
    print("\n".join(executed))


if __name__ == "__main__":
    main()
