from __future__ import annotations

import json
import os
import subprocess
import sys
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

import pytest
from app.db.base import Base
from app.db.session import sqlalchemy_database_url
from scripts import crm_reconcile_readonly as cli
from sqlalchemy import create_engine, text


class _ReadOnlySheetsFixture:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.reads = 0
        self.mutations = 0

    def read_crm_contacts_chunk(self, *, start_row: int, limit: int = 100) -> list[list[str]]:
        del start_row, limit
        self.reads += 1
        if self.fail:
            raise RuntimeError("private provider failure")
        return []

    def read_crm_activity_chunk(self, *, start_row: int, limit: int = 100) -> list[list[str]]:
        return self.read_crm_contacts_chunk(start_row=start_row, limit=limit)

    def ensure_crm_workspace(self) -> None:
        self.mutations += 1
        raise AssertionError("mutation forbidden")

    def upsert_crm_contact(self, _cells) -> None:  # noqa: ANN001
        self.mutations += 1
        raise AssertionError("mutation forbidden")

    append_crm_contact = upsert_crm_contact
    upsert_crm_activity = upsert_crm_contact
    append_crm_activity = upsert_crm_contact


def _sqlite_fixture(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    path = tmp_path / "diagnostics.sqlite3"
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("MIA_DATABASE_URL", f"sqlite+pysqlite:///{path}")


def test_with_sheets_exposes_only_reads_and_never_mutates(
    tmp_path, monkeypatch, capsys
) -> None:
    _sqlite_fixture(tmp_path, monkeypatch)
    sheets = _ReadOnlySheetsFixture()
    monkeypatch.setattr(cli, "build_sheets_port", lambda _settings: sheets)

    result = cli.main(["--environment", "test", "--with-sheets", "--max-rows", "10"])
    payload = json.loads(capsys.readouterr().out)

    assert result == 0
    assert payload["ok"] is True
    assert payload["report"]["sheets"]["contacts"]["status"] == "partial"
    assert sheets.reads == 2
    assert sheets.mutations == 0


def test_sheet_provider_failure_is_sanitized_and_partial(
    tmp_path, monkeypatch, capsys
) -> None:
    _sqlite_fixture(tmp_path, monkeypatch)
    sheets = _ReadOnlySheetsFixture(fail=True)
    monkeypatch.setattr(cli, "build_sheets_port", lambda _settings: sheets)

    result = cli.main(["--environment", "test", "--with-sheets", "--max-rows", "10"])
    output = capsys.readouterr().out
    payload = json.loads(output)

    assert result == 0
    assert payload["report"]["sheets"]["contacts"]["status"] == "read_failed"
    assert "private provider failure" not in output
    assert sheets.mutations == 0


def test_module_invocation_is_supported() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "scripts.crm_reconcile_readonly", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0
    assert "--with-sheets" in completed.stdout


def _schema_url(raw_url: str, schema: str) -> str:
    plain = raw_url.replace("postgresql+psycopg://", "postgresql://", 1)
    parts = urlsplit(plain)
    option = f"options={quote(f'-csearch_path={schema}') }"
    query = f"{parts.query}&{option}" if parts.query else option
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


def test_plain_postgres_dsn_runs_in_read_only_isolated_schema(
    monkeypatch, capsys
) -> None:
    raw_url = os.environ.get("MIA_TEST_POSTGRES_URL", "").strip()
    if not raw_url:
        pytest.skip("MIA_TEST_POSTGRES_URL is not configured")
    schema = "mia_diag_" + uuid4().hex
    admin = create_engine(sqlalchemy_database_url(raw_url))
    with admin.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        scoped_url = _schema_url(raw_url, schema)
        engine = create_engine(sqlalchemy_database_url(scoped_url))
        Base.metadata.create_all(engine)
        monkeypatch.setenv("MIA_DATABASE_URL", scoped_url)

        result = cli.main(
            [
                "--environment",
                "test",
                "--allow-network-database-readonly",
                "--max-rows",
                "10",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert result == 0
        assert payload["ok"] is True
        assert payload["report"]["read_only"] is True
    finally:
        with admin.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
