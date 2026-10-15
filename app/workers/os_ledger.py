"""Owner-invoked migration job. No settings loader or default database fallback."""

import argparse
import json
import os
import re

from sqlalchemy import create_engine
from sqlalchemy.exc import SQLAlchemyError

from app.db.os_ledger_migrate import apply_ledger_migrations, validate_main_role
from app.db.session import sqlalchemy_database_url

REFUSAL_CODES = frozenset({
    "postgres_required", "ledger_migrations_missing", "ledger_migration_not_allowlisted",
    "ledger_migration_requires_lf", "ledger_owner_unsafe", "ledger_owner_membership_unsafe",
    "ledger_schema_owner_unsafe", "ledger_migration_history_unknown",
    "ledger_migration_digest_mismatch", "app_role_demote_required",
    "app_role_database_owner_unsafe", "app_role_membership_unsafe",
    "app_role_ledger_access_unsafe", "ledger_database_mismatch", "ledger_main_dsn_not_separate",
    "app_role_impersonation_unsafe",
})


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
    except Exception as exc:
        # No exception/provider text or connection URL is printed by this privileged job.
        code = ""
        if isinstance(exc, ValueError) and len(exc.args) == 1:
            candidate = exc.args[0]
            if isinstance(candidate, str) and candidate in REFUSAL_CODES:
                code = candidate
        elif isinstance(exc, SQLAlchemyError):
            candidate = getattr(getattr(exc, "orig", None), "sqlstate", None)
            if isinstance(candidate, str) and re.fullmatch(r"[0-9A-Z]{5}", candidate):
                code = candidate
        parser.exit(1, "ledger_migration_refused" + (f":{code}" if code else "") + "\n")
    finally:
        for engine in engines:
            engine.dispose()


if __name__ == "__main__":
    main()
