"""Local opt-in SQL migration proof, without ORM create_all masking the migration."""

import os
from pathlib import Path
from uuid import uuid4

import pytest
from app.db.migrate import apply_migrations
from app.db.reminders import OwnerReminderRow
from sqlalchemy import create_engine, inspect, text


def test_reminder_sql_migration_is_additive_and_repeatable(monkeypatch):
    url = os.environ.get("MIA_TEST_POSTGRES_URL", "")
    if not url:
        pytest.skip("MIA_TEST_POSTGRES_URL required for local PostgreSQL proof")
    admin = create_engine(url)
    schema = "reminder_migration_" + uuid4().hex
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    root = Path(__file__).resolve().parents[2]
    paths = [
        root / "migrations/20260910_crm_v2.sql",
        root / "migrations/20261011_owner_reminders.sql",
    ]
    monkeypatch.setattr("app.db.migrate.list_migration_files", lambda: paths)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE owner_tasks (id INTEGER PRIMARY KEY, due_at TEXT)"))
            conn.execute(text("INSERT INTO owner_tasks VALUES (1, '2026-10-11')"))
        assert not apply_migrations(engine).failed
        assert not apply_migrations(engine).applied
        columns = {item["name"]: item for item in inspect(engine).get_columns("owner_reminders")}
        assert set(columns) == set(OwnerReminderRow.__table__.columns.keys())
        assert columns["scheduled_at"]["type"].timezone is True
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT due_at FROM owner_tasks WHERE id=1")) == "2026-10-11"
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()
