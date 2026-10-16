"""V2-29 / §22 E9: the mirror workbook is never a Mia write target, and the old OS workbook stops
being one once T1 starts. Any coerced or unexpected value fails toward subtracting."""

from __future__ import annotations

from typing import Any

import pytest
from app.core import config
from app.core.config import Settings


@pytest.fixture(autouse=True)
def fresh_coercion_log(monkeypatch):
    monkeypatch.setattr(config, "_OS_CONFIG_COERCED", set())


def _settings(**overrides: Any) -> Settings:
    return Settings(
        _env_file=None,
        sheets_allowed_spreadsheet_ids="extra-1,os-book,mirror-book",
        assaf_os_workbook_id="os-book",
        **overrides,
    )


def _allowed(**overrides: Any) -> frozenset[str]:
    return _settings(**overrides).allowed_sheets_spreadsheet_ids()


def test_the_mirror_workbook_is_never_allowed() -> None:
    assert "mirror-book" in _allowed()
    for overrides in (
        {"os_mirror_spreadsheet_id": "mirror-book"},
        {"os_mirror_spreadsheet_id": " mirror-book ", "assaf_os_backend": "sheets"},
        {"os_mirror_spreadsheet_id": "mirror-book", "assaf_os_backend": "postgres"},
        {"os_mirror_spreadsheet_id": "mirror-book", "assaf_os_freeze": True},
        {"os_mirror_spreadsheet_id": "mirror-book", "sheets_spreadsheet_id": "mirror-book"},
    ):
        assert "mirror-book" not in _allowed(**overrides), overrides
    assert "extra-1" in _allowed(os_mirror_spreadsheet_id="mirror-book")


def test_the_os_workbook_is_subtracted_once_t1_starts() -> None:
    assert "os-book" in _allowed()
    assert "os-book" in _allowed(assaf_os_backend="sheets", assaf_os_freeze=False)
    assert "os-book" not in _allowed(assaf_os_freeze=True)
    assert "os-book" not in _allowed(assaf_os_backend="postgres")
    assert "os-book" not in _allowed(assaf_os_backend="postgres", assaf_os_freeze=True)
    assert "os-book" not in _allowed(assaf_os_freeze="true")
    assert "extra-1" in _allowed(assaf_os_freeze=True, os_mirror_spreadsheet_id="mirror-book")


@pytest.mark.parametrize(
    "overrides",
    [
        {"assaf_os_backend": "postgresql"},
        {"assaf_os_backend": "bogus"},
        {"assaf_os_backend": 7},
        {"assaf_os_freeze": "maybe"},
        {"assaf_os_freeze": 2},
    ],
)
def test_a_coerced_value_fails_toward_subtracting_the_os_workbook(overrides) -> None:
    allowed = _allowed(**overrides)
    assert config.os_config_coerced() is True
    assert "os-book" not in allowed
    assert "extra-1" in allowed


def test_an_unexpected_value_set_after_load_also_subtracts() -> None:
    settings = _settings()
    assert "os-book" in settings.allowed_sheets_spreadsheet_ids()
    settings.assaf_os_backend = "Postgres"
    assert "os-book" not in settings.allowed_sheets_spreadsheet_ids()
    settings = _settings()
    settings.assaf_os_freeze = "no"  # type: ignore[assignment]
    assert "os-book" not in settings.allowed_sheets_spreadsheet_ids()


def test_an_unrelated_coercion_keeps_the_os_workbook_while_still_on_sheets() -> None:
    allowed = _allowed(os_mirror_lag_warn_seconds=5)
    assert config.os_config_coerced() is True
    assert "os-book" in allowed
