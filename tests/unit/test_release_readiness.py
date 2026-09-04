"""The release must be verifiable from production, and safe to run in order.

Three things this batch depends on: /health says exactly which commit is serving,
the schema version is readable, and the migration that the new website turn needs is
safe to run before the code that uses it.
"""

from __future__ import annotations

import pathlib

from app.core.config import Settings
from app.db.migrate import applied_schema_version, apply_migrations
from app.db.session import get_engine, init_db
from app.main import _deployment_block

MIGRATION = "20260904_website_session_state.sql"


def test_health_reports_the_commit_that_was_built() -> None:
    block = _deployment_block(Settings(_env_file=None, build_sha="abc123def456"))
    assert block["commit_sha"] == "abc123def456"


def test_an_unstamped_image_reports_no_commit_rather_than_guessing() -> None:
    """Empty is honest and fails the smoke check. A runtime guess would be a lie."""
    assert _deployment_block(Settings(_env_file=None))["commit_sha"] == ""


def test_health_carries_the_versions_needed_to_identify_a_release() -> None:
    block = _deployment_block(Settings(_env_file=None, build_sha="deadbeef"))
    for key in ("commit_sha", "env", "app_version", "prompt_version", "schema_version"):
        assert key in block, key
    assert block["prompt_version"].startswith("sales_reply_v")


def test_health_deployment_leaks_no_secret() -> None:
    settings = Settings(
        _env_file=None,
        build_sha="abc123",
        openai_api_key="sk-SECRET-must-not-appear",
        composio_api_key="cmp-SECRET-must-not-appear",
        telegram_bot_token="tg-SECRET-must-not-appear",
    )
    blob = repr(_deployment_block(settings))
    for secret in ("sk-SECRET", "cmp-SECRET", "tg-SECRET", "postgres", "://"):
        assert secret not in blob, secret


def test_the_website_session_migration_ships_in_this_release() -> None:
    path = pathlib.Path("migrations") / MIGRATION
    assert path.is_file(), "the website turn reads and writes website_session_state"
    body = path.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS website_session_state" in body


def test_migrations_are_safe_to_run_twice() -> None:
    """The runner records each file, so a rerun applies nothing and does not fail."""
    init_db()
    engine = get_engine()
    first = apply_migrations(engine)
    second = apply_migrations(engine)
    assert not first.failed
    assert not second.failed
    assert second.applied == [], "a second run must apply nothing"


def test_the_schema_version_is_readable_for_health() -> None:
    init_db()
    engine = get_engine()
    apply_migrations(engine)
    version = applied_schema_version(engine)
    assert isinstance(version, str)


def test_the_schema_version_never_raises_on_a_broken_engine() -> None:
    """/health must answer even when the database is unreachable."""

    class Broken:
        def connect(self):  # noqa: ANN201
            raise RuntimeError("no database")

    assert applied_schema_version(Broken()) == ""
