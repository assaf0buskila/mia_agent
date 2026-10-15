"""Owner-invoked migration job. No settings loader or default database fallback."""

import argparse
import json
import os
import re

from sqlalchemy import create_engine

from app.db.os_ledger_migrate import apply_ledger_migrations, validate_main_role
from app.db.session import sqlalchemy_database_url


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mia os-ledger")
    parser.add_argument("command", choices=("migrate",))
    parser.add_argument("--approved-sha", required=True)
    args = parser.parse_args(argv)
    build_sha = os.getenv("MIA_BUILD_SHA", "")
    if not re.fullmatch(r"[0-9a-f]{40}", build_sha) or args.approved_sha != build_sha:
        parser.exit(1, "ledger_owner_go_sha_required\n")
    main_url = os.getenv("MIA_DATABASE_URL", "")
    migration_url = os.getenv("MIA_OS_LEDGER_MIGRATION_DATABASE_URL", "")
    if not main_url or not migration_url:
        parser.exit(1, "ledger_explicit_dsns_required\n")
    engines = []
    try:
        for url in (main_url, migration_url):
            engines.append(create_engine(sqlalchemy_database_url(url)))
        main_role = validate_main_role(engines[0], engines[1])
        summary = apply_ledger_migrations(engines[1], main_role=main_role)
        print(json.dumps(summary.model_dump()))
    except Exception:
        # No exception/provider text or connection URL is printed by this privileged job.
        parser.exit(1, "ledger_migration_refused\n")
    finally:
        for engine in engines:
            engine.dispose()


if __name__ == "__main__":
    main()
