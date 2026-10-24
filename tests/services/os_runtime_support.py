"""OS-RUNTIME test support: a store whose sessions never share a connection.

A file-backed SQLite database or a disposable PostgreSQL schema gives every session its own
connection, so a probe session sees only COMMITTED rows. An in-memory StaticPool shares one
connection between all sessions and would prove nothing about commit order.

The factory is built with the production session settings (``autoflush=False``,
``expire_on_commit`` at its default), as ``app/db/session.py:37`` does.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from uuid import uuid4

import pytest
from app.db.base import Base
from app.db.models import ApprovalRow
from app.db.session import make_engine
from app.services.owner_actions import read_owner_action
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker

FOREIGN_WRITERS_SQL = (
    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
    "AND pid <> pg_backend_pid() AND backend_xid IS NOT NULL"
)


def open_factory(kind: str, tmp_path, name: str) -> tuple[sessionmaker, Callable[[], None]]:
    """``(factory, close)`` for ``kind`` in ``{"sqlite", "postgres"}``; skips PG without a DSN."""
    admin = None
    schema = f"{name}_{uuid4().hex}"
    if kind == "postgres":
        url = os.environ.get("MIA_TEST_POSTGRES_URL", "")
        if not url:
            pytest.skip("MIA_TEST_POSTGRES_URL required")
        admin = create_engine(url)
        with admin.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    else:
        engine = make_engine(f"sqlite:///{tmp_path / (name + '.db')}")
    Base.metadata.create_all(engine, checkfirst=kind != "postgres")
    factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    factory.dialect_name = kind  # type: ignore[attr-defined]

    def close() -> None:
        engine.dispose()
        if admin is not None:
            with admin.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
            admin.dispose()

    return factory, close


def foreign_write_xids(factory: sessionmaker) -> int | None:
    """Backends other than this probe that hold a write transaction (PostgreSQL only)."""
    if getattr(factory, "dialect_name", "") != "postgres":
        return None
    with factory() as fresh:
        return fresh.execute(text(FOREIGN_WRITERS_SQL)).scalar()


def visible_owner_actions(factory: sessionmaker, kind: str) -> int:
    """Committed ApprovalRows of one owner-action kind, read through a brand-new session."""
    with factory() as fresh:
        return sum(
            1
            for row in fresh.scalars(select(ApprovalRow)).all()
            if (read_owner_action(row) or {}).get("kind") == kind
        )
