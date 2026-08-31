"""Apply additive SQL migrations from migrations/*.sql in filename order."""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

POSTGRES_ONLY = frozenset(
    {
        "20260821_approval_campaign_resource.sql",
        "20260901_approval_resource_id_varchar80.sql",
        "20260901_linkedin_approval_parameters_text.sql",
    }
)

_SCHEMA_MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  filename VARCHAR(255) PRIMARY KEY,
  applied_at VARCHAR(64) NOT NULL
)
"""


class MigrationSummary(BaseModel):
    applied: list[str] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)
    already: list[str] = Field(default_factory=list)
    failed: str = ""


def migrations_dir_for(
    module_file: Path,
    *,
    image_dir: Path = Path("/app/migrations"),
    cwd: Path | None = None,
) -> Path:
    """SQL files live at repo root (editable) or `/app/migrations` (prod image).

    `uv sync --no-editable` puts this module in site-packages, so parents[2]
    is not the image workdir. Prefer a directory that actually contains `*.sql`.
    """
    here = module_file.resolve()
    candidates = [
        here.parents[2] / "migrations",
        image_dir,
        (cwd or Path.cwd()) / "migrations",
    ]
    for path in candidates:
        if path.is_dir() and next(path.glob("*.sql"), None) is not None:
            return path
    return candidates[0]


def _migrations_dir() -> Path:
    return migrations_dir_for(Path(__file__))


def list_migration_files() -> list[Path]:
    return sorted(_migrations_dir().glob("*.sql"), key=lambda path: path.name)


def _split_statements(sql: str) -> list[str]:
    statements: list[str] = []
    for chunk in sql.split(";"):
        lines: list[str] = []
        for line in chunk.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            lines.append(line)
        statement = "\n".join(lines).strip()
        if statement:
            statements.append(statement)
    return statements


def _is_duplicate_schema_error(exc: SQLAlchemyError) -> bool:
    message = str(exc).lower()
    return "duplicate column" in message or "already exists" in message


def _ensure_schema_migrations_table(conn) -> None:
    conn.execute(text(_SCHEMA_MIGRATIONS_DDL))


def _applied_filenames(conn) -> set[str]:
    rows = conn.execute(text("SELECT filename FROM schema_migrations")).fetchall()
    return {row[0] for row in rows}


def _record_migration(conn, filename: str) -> None:
    applied_at = datetime.now(UTC).isoformat()
    conn.execute(
        text(
            "INSERT INTO schema_migrations (filename, applied_at) "
            "VALUES (:filename, :applied_at)"
        ),
        {"filename": filename, "applied_at": applied_at},
    )


def _apply_file(conn, path: Path) -> None:
    statements = _split_statements(path.read_text(encoding="utf-8"))
    for statement in statements:
        try:
            # Postgres aborts the whole transaction on a failed ALTER; a
            # savepoint lets later statements (and duplicate-column skips) run.
            with conn.begin_nested():
                conn.execute(text(statement))
        except SQLAlchemyError as exc:
            if _is_duplicate_schema_error(exc):
                continue
            raise


def apply_migrations(engine: Engine) -> MigrationSummary:
    summary = MigrationSummary()
    is_sqlite = engine.dialect.name == "sqlite"

    with engine.begin() as conn:
        _ensure_schema_migrations_table(conn)

    with engine.connect() as conn:
        applied = _applied_filenames(conn)

    for path in list_migration_files():
        filename = path.name

        if is_sqlite and filename in POSTGRES_ONLY:
            summary.skipped.append(filename)
            continue

        if filename in applied:
            summary.already.append(filename)
            continue

        try:
            with engine.begin() as conn:
                _apply_file(conn, path)
                _record_migration(conn, filename)
            summary.applied.append(filename)
            applied.add(filename)
        except SQLAlchemyError as exc:
            orig = getattr(exc, "orig", None)
            sqlstate = getattr(orig, "sqlstate", None) or type(exc).__name__
            print(f"migration_sqlstate {filename} {sqlstate}", file=sys.stderr)
            summary.failed = filename
            break
        except Exception:
            summary.failed = filename
            break

    return summary
