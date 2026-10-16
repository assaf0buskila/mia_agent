"""Print a pinned ledger role plan. Production targets are deliberately not enrolled.

Run from the repo: uv run --offline python ops/assaf-os/provision_ledger.py
--target local-v2-15. Applying is a separate owner-approved operation. This does
not set passwords, demote the main role, migrate tables, or activate any feature.
"""

import argparse
import os
import re
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

# Allow direct execution without installing another console entrypoint.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.db.os_ledger_migrate import validate_main_role  # noqa: E402
from app.services.os_ledger.role_policy import (  # noqa: E402
    ledger_grant_statements,
    validate_ledger_catalog,
)

# Adding a production target requires a reviewed pin and the separate owner go.
# Host, port, database, migration identity and main identity are all exact pins.
_TARGETS = {"local-v2-15": ("127.0.0.1", 56215, "mia_ledger_test", "postgres", "v215_main")}


def provision_statements(database: str) -> tuple[str, ...]:
    """Return exact DDL + GRANT plan; ValueError('ledger_target_unpinned') on unknown DB."""
    if database not in {pin[2] for pin in _TARGETS.values()}:
        raise ValueError("ledger_target_unpinned")
    posture = "NOINHERIT NOSUPERUSER NOCREATEROLE NOCREATEDB NOREPLICATION NOBYPASSRLS"
    return (
        f"CREATE ROLE assaf_os_ledger LOGIN {posture};",
        f"CREATE ROLE assaf_os_retention NOLOGIN {posture};",
        # PostgreSQL grants TEMP through PUBLIC by default. Revoking only from
        # the role cannot cancel that effective right. Explicitly show the scope.
        f"REVOKE TEMPORARY ON DATABASE {database} FROM PUBLIC;",
        *ledger_grant_statements(),
    )


def main(argv: list[str] | None = None) -> None:
    """Dry-run CLI. SystemExit 2 on arguments; 1 with sanitized refusal code on apply."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True, choices=tuple(_TARGETS))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-sha")
    args = parser.parse_args(argv)
    host, port, database, migration_role, main_role = _TARGETS[args.target]
    statements = provision_statements(database)
    if not args.apply:
        print("\n".join(statements))
        return
    sha = os.environ.get("MIA_BUILD_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", sha) or args.approved_sha != sha:
        parser.exit(1, "ledger_owner_go_sha_required\n")
    engines = []
    try:
        for variable, role in (
            ("MIA_OS_LEDGER_MIGRATION_DATABASE_URL", migration_role),
            ("MIA_DATABASE_URL", main_role),
        ):
            url = make_url(os.environ.get(variable, ""))
            binding = (
                url.drivername, url.host, url.port, url.database, url.username, dict(url.query),
            )
            if binding != (
                "postgresql+psycopg", host, port, database, role, {},
            ):
                raise ValueError("ledger_target_unpinned")
            engines.append(create_engine(url, connect_args={"connect_timeout": 10}))
        actual_main = validate_main_role(engines[1], engines[0])
        with engines[0].begin() as conn:
            conn.execute(text("SET LOCAL statement_timeout = '5s'"))
            conn.execute(text("SET LOCAL lock_timeout = '5s'"))
            conn.execute(text("SELECT pg_advisory_xact_lock(734927, 14)"))
            identity = conn.execute(text(
                "SELECT current_database(),session_user,current_user"
            )).one()
            # The DSN endpoint is pinned above. A disposable Docker port mapping
            # changes inet_server_addr/port; validate_main_role already proves
            # that both real connections reach the same server and database.
            if tuple(identity) != (database, migration_role, migration_role):
                raise ValueError("ledger_target_unpinned")
            # CREATE ROLE deliberately refuses existing roles; never adopt,
            # broaden or clean up somebody else's role or privileges.
            for statement in statements:
                conn.execute(text(statement))
            validate_ledger_catalog(conn, main_role=actual_main, migration_role=migration_role)
        print("\n".join(statements))
    except Exception:
        # Never print DSNs, driver exceptions or provider/catalog payloads.
        parser.exit(1, "ledger_provision_refused\n")
    finally:
        for engine in engines:
            engine.dispose()


if __name__ == "__main__":
    main()
