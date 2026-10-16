import logging
from types import SimpleNamespace

import pytest
from app.core.config import Settings


def test_default_is_observe():
    assert Settings(_env_file=None).app_role_posture == "observe"


@pytest.mark.parametrize(
    "reason",
    [
        "app_role_demote_required",
        "app_role_ledger_access_unsafe",
        "app_role_ok",
        "app_role_check_failed",
        "app_role_database_owner_unsafe",
        "app_role_membership_unsafe",
    ],
)
@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_readiness_matrix(monkeypatch, caplog, reason, mode):
    from app.db import app_role_posture as posture

    monkeypatch.setattr(posture, "inspect_posture", lambda engine: reason)
    caplog.set_level(logging.INFO)
    assert posture.posture_ready(object(), mode) == (mode == "observe" or reason == "app_role_ok")
    assert reason in caplog.text
    assert "postgresql" not in caplog.text


@pytest.mark.parametrize("mode,expected", [("observe", True), ("enforce", False)])
def test_error_is_sanitized(monkeypatch, caplog, mode, expected):
    from app.db import app_role_posture as posture

    def fail(engine):
        raise RuntimeError("postgresql://secret:password@host/database")

    monkeypatch.setattr(posture, "inspect_posture", fail)
    caplog.set_level(logging.INFO)
    assert posture.posture_ready(object(), mode) is expected
    assert "app_role_check_failed" in caplog.text
    assert "secret" not in caplog.text and "password" not in caplog.text


def test_ready_endpoint_uses_posture(monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "database_ready", lambda: True)
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(app_role_posture="enforce"))
    monkeypatch.setattr(main, "app_role_ready", lambda mode: False)
    assert main.health_ready().status_code == 503
    monkeypatch.setattr(main, "app_role_ready", lambda mode: True)
    assert main.health_ready().status_code == 200
