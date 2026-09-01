"""Truncate operational data while keeping schema and migration history."""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError

_PRESERVE_TABLES = frozenset({"schema_migrations"})


def wipe_all_data(engine: Engine) -> list[str]:
    """Delete every row in public tables except ``schema_migrations``.

    Returns table names cleared, sorted. Schema and applied migration filenames stay.
    """
    if engine.dialect.name == "postgresql":
        return _wipe_postgres(engine)
    return _wipe_sqlite(engine)


def _wipe_postgres(engine: Engine) -> list[str]:
    wiped: list[str] = []
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT tablename FROM pg_tables "
                "WHERE schemaname = 'public' AND tablename <> 'schema_migrations'"
            )
        ).fetchall()
        for name in sorted(row[0] for row in rows):
            conn.execute(text(f'TRUNCATE TABLE "{name}" RESTART IDENTITY CASCADE'))
            wiped.append(name)
    return wiped


def _wipe_sqlite(engine: Engine) -> list[str]:
    from app.db import models as _models  # noqa: F401
    from app.db.base import Base

    wiped: list[str] = []
    inspector = inspect(engine)
    table_names = [
        table.name
        for table in reversed(Base.metadata.sorted_tables)
        if table.name not in _PRESERVE_TABLES and inspector.has_table(table.name)
    ]
    with engine.begin() as conn:
        conn.execute(text("PRAGMA foreign_keys = OFF"))
        for name in table_names:
            try:
                conn.execute(text(f'DELETE FROM "{name}"'))
                wiped.append(name)
            except SQLAlchemyError:
                continue
        conn.execute(text("PRAGMA foreign_keys = ON"))
    return sorted(wiped)
