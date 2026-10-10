"""Print a sanitized CRM diagnostic report from an explicitly selected database."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib.parse import quote

from app.core.config import Settings
from app.db.session import sqlalchemy_database_url
from app.integrations.sheets import DisabledSheetsPort, build_sheets_port
from app.services.crm_diagnostics import build_crm_diagnostics
from app.services.phone_identity import normalize_new_input_phone
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--environment", required=True, choices=("dev", "test", "prod"))
    parser.add_argument(
        "--database-url-env",
        default="MIA_DATABASE_URL",
        help="Name of the already-populated environment variable containing the DSN.",
    )
    parser.add_argument(
        "--allow-network-database-readonly",
        action="store_true",
        help="Required for PostgreSQL. The transaction is repeatable-read and read-only.",
    )
    parser.add_argument(
        "--allow-production-readonly",
        action="store_true",
        help="Additional explicit gate when --environment=prod.",
    )
    parser.add_argument(
        "--with-sheets",
        action="store_true",
        help=(
            "Explicitly enable bounded read-only CRM Sheet reads using configured "
            "Composio credentials. No workspace creation, import, sync, or write occurs."
        ),
    )
    parser.add_argument("--max-rows", type=int, default=5_000)
    return parser


def _emit_error(code: str) -> int:
    print(json.dumps({"ok": False, "error_code": code}, sort_keys=True))
    return 2


def _sqlite_engine(database_url: str):
    parsed = make_url(database_url)
    database = parsed.database or ""
    if not database or database == ":memory:":
        raise ValueError("sqlite_file_required")
    path = Path(database).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("sqlite_file_missing")

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(
            f"file:{quote(path.as_posix(), safe='/:')}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        connection.execute("PRAGMA query_only = ON")
        return connection

    return create_engine("sqlite+pysqlite://", creator=connect)


class _ReadOnlySheets:
    """Expose only the two bounded CRM read methods to diagnostics."""

    def __init__(self, port: object) -> None:
        self._contacts = getattr(port, "read_crm_contacts_chunk")
        self._activity = getattr(port, "read_crm_activity_chunk")

    def read_crm_contacts_chunk(self, *, start_row: int, limit: int = 100) -> list[list[str]]:
        return self._contacts(start_row=start_row, limit=limit)

    def read_crm_activity_chunk(self, *, start_row: int, limit: int = 100) -> list[list[str]]:
        return self._activity(start_row=start_row, limit=limit)


def _diagnostic_kwargs(settings: Settings, *, with_sheets: bool, max_rows: int) -> dict[str, Any]:
    values: dict[str, Any] = {
        "max_rows": max_rows,
        "phone_normalizer": normalize_new_input_phone,
        "recipient_ids": settings.telegram_owner_user_id_set(),
    }
    if not with_sheets:
        return values
    port = build_sheets_port(settings)
    if isinstance(port, DisabledSheetsPort):
        raise RuntimeError("sheets_not_configured")
    values["sheets"] = _ReadOnlySheets(port)
    return values


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    variable = str(args.database_url_env or "")
    if variable != "MIA_DATABASE_URL":
        return _emit_error("database_env_name_not_allowed")
    database_url = os.environ.get(variable, "").strip()
    if not database_url:
        return _emit_error("database_url_missing")
    if args.environment == "prod" and not args.allow_production_readonly:
        return _emit_error("production_read_not_authorized")
    engine = None
    try:
        normalized_database_url = sqlalchemy_database_url(database_url)
        settings = Settings(
            _env_file=None,
            env=args.environment,
            database_url=normalized_database_url,
        )
        parsed = make_url(settings.database_url)
        backend = parsed.get_backend_name()
        if backend == "postgresql" and not args.allow_network_database_readonly:
            return _emit_error("network_database_not_authorized")
        if backend not in {"sqlite", "postgresql"}:
            return _emit_error("database_backend_not_supported")
        diagnostic_kwargs = _diagnostic_kwargs(
            settings,
            with_sheets=args.with_sheets,
            max_rows=args.max_rows,
        )
        if backend == "sqlite":
            engine = _sqlite_engine(settings.database_url)
            with engine.connect() as connection:
                transaction = connection.begin()
                try:
                    with Session(connection, autoflush=False) as session:
                        report = build_crm_diagnostics(
                            session,
                            **diagnostic_kwargs,
                        )
                finally:
                    transaction.rollback()
        elif backend == "postgresql":
            engine = create_engine(settings.database_url)
            with engine.connect().execution_options(
                isolation_level="REPEATABLE READ"
            ) as connection:
                transaction = connection.begin()
                try:
                    connection.execute(text("SET TRANSACTION READ ONLY"))
                    with Session(connection, autoflush=False) as session:
                        report = build_crm_diagnostics(
                            session,
                            **diagnostic_kwargs,
                        )
                finally:
                    transaction.rollback()
    except FileNotFoundError:
        return _emit_error("sqlite_file_missing")
    except RuntimeError as exc:
        if str(exc) == "sheets_not_configured":
            return _emit_error("sheets_not_configured")
        return _emit_error("diagnostic_read_failed")
    except ValueError:
        return _emit_error("invalid_runtime_configuration")
    except Exception:  # noqa: BLE001 - exception/provider text may contain secrets
        return _emit_error("diagnostic_read_failed")
    finally:
        if engine is not None:
            engine.dispose()
    print(json.dumps({"ok": True, "report": report}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
