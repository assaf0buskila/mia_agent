"""Opt-in migration proof: SQL must create v2 tables without create_all masking it."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from app.db import models, site_v2  # noqa: F401 - register every mapped table
from app.db.base import Base
from app.db.migrate import apply_migrations
from sqlalchemy import create_engine, inspect, text


def test_v2_sql_creates_schema_and_preserves_existing_contact_history(monkeypatch):
    url = os.environ.get("MIA_TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("MIA_TEST_POSTGRES_URL is required for isolated PostgreSQL proof")
    schema = "mia_v2_migration_" + uuid4().hex
    admin = create_engine(url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    try:
        root = Path(__file__).resolve().parents[2]
        paths = sorted((root / "migrations").glob("20260910_*v2.sql"))
        assert {p.name for p in paths} >= {"20260910_crm_v2.sql", "20260910_site_v2.sql"}
        monkeypatch.setattr("app.db.migrate.list_migration_files", lambda: paths)
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE legacy_history (id INTEGER PRIMARY KEY, body TEXT)"))
            conn.execute(text("INSERT INTO legacy_history VALUES (1, 'historical conversation')"))
        first = apply_migrations(engine)
        assert not first.failed
        assert first.applied == [p.name for p in paths]
        inspector = inspect(engine)
        v2_tables = [
            t for t in Base.metadata.sorted_tables if t.name.startswith(("crm_", "site_v2_"))
        ]
        assert v2_tables
        for table in v2_tables:
            actual = {column["name"] for column in inspector.get_columns(table.name)}
            assert actual == set(table.columns.keys()), table.name
        again = apply_migrations(engine)
        assert not again.failed and not again.applied
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT body FROM legacy_history WHERE id=1")) == (
                "historical conversation"
            )
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
