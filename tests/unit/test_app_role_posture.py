import logging
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from app.core.config import Settings

BAD_VALUES = ["bogus", "ENFORCE", "Enforce", "enforce ", " enforce", "Observe", "true", "1", ""]
COERCED = "app_role_posture op=load outcome=coerced reason=config_invalid"


def test_default_is_observe():
    assert Settings(_env_file=None).app_role_posture == "observe"


def test_setting_is_a_plain_str_without_constraints():
    # Section 3.3: no Literal and no validator that can fail the settings load.
    field = Settings.model_fields["app_role_posture"]
    assert field.annotation is str
    assert field.default == "observe"
    assert not field.metadata


@pytest.mark.parametrize("value", BAD_VALUES)
def test_bad_value_never_raises_at_settings_load(monkeypatch, value):
    monkeypatch.setenv("MIA_APP_ROLE_POSTURE", value)
    assert Settings(_env_file=None).app_role_posture == value
    monkeypatch.delenv("MIA_APP_ROLE_POSTURE")
    assert Settings(_env_file=None, app_role_posture=value).app_role_posture == value


@pytest.mark.parametrize("value", BAD_VALUES + [None])
def test_unknown_value_behaves_as_observe_at_use(monkeypatch, caplog, value):
    from app.db import app_role_posture as posture

    monkeypatch.setattr(posture, "_coerced_logged", False)
    monkeypatch.setattr(posture, "inspect_posture", lambda engine: "app_role_demote_required")
    caplog.set_level(logging.INFO)
    assert posture.effective_mode(value) == "observe"
    # Observe never blocks readiness, even when the posture is unsafe.
    assert posture.posture_ready(object(), value) is True
    assert "ready=True" in caplog.text


@pytest.mark.parametrize("value", BAD_VALUES + [None])
def test_unknown_value_behaves_as_observe_without_an_engine(monkeypatch, value):
    from app.db import app_role_posture as posture
    from app.db import session

    def broken():
        raise RuntimeError("no engine")

    monkeypatch.setattr(posture, "_coerced_logged", False)
    monkeypatch.setattr(session, "get_engine", broken)
    assert posture.app_role_ready(value) is True
    assert posture.app_role_ready("enforce") is False


def test_only_exactly_enforce_enforces(monkeypatch, caplog):
    from app.db import app_role_posture as posture

    monkeypatch.setattr(posture, "_coerced_logged", False)
    caplog.set_level(logging.INFO)
    assert posture.effective_mode("enforce") == "enforce"
    assert posture.effective_mode("observe") == "observe"
    assert COERCED not in caplog.text


def test_coercion_is_logged_once_and_never_echoes_the_value(monkeypatch, caplog):
    from app.db import app_role_posture as posture

    monkeypatch.setattr(posture, "_coerced_logged", False)
    caplog.set_level(logging.INFO)
    for value in ("hunter2-secret", "ENFORCE", None):
        assert posture.effective_mode(value) == "observe"
    assert posture.posture_ready(object(), "again") in (True, False)
    messages = [
        record.getMessage() for record in caplog.records if "op=load" in record.getMessage()
    ]
    assert messages == [COERCED]
    assert "hunter2-secret" not in caplog.text


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


@pytest.mark.parametrize("value,status", [("bogus", 200), ("ENFORCE", 200), ("enforce", 503)])
def test_ready_endpoint_treats_a_bad_setting_as_observe(monkeypatch, value, status):
    import app.main as main
    from app.db import app_role_posture as posture
    from app.db import session

    monkeypatch.setattr(posture, "_coerced_logged", False)
    monkeypatch.setattr(main, "database_ready", lambda: True)
    monkeypatch.setattr(main, "get_settings", lambda: SimpleNamespace(app_role_posture=value))
    monkeypatch.setattr(session, "get_engine", lambda: object())
    monkeypatch.setattr(posture, "inspect_posture", lambda engine: "app_role_demote_required")
    assert main.health_ready().status_code == status


def test_posture_check_imports_the_real_ledger_validator():
    from app.db import app_role_posture as posture
    from app.db import os_ledger_migrate

    assert posture._validate_main_role_catalog is os_ledger_migrate._validate_main_role_catalog
    # The catalog SQL is V2-14's alone. A copy here would drift from the real check.
    source = open(posture.__file__, encoding="utf-8").read()
    assert "has_any_column_privilege" not in source
    assert "has_sequence_privilege" not in source
    assert not hasattr(posture, "_ROLE_SQL") and not hasattr(posture, "_LEDGER_SQL")


class _FakeConnection:
    def __init__(self, identity):
        self.identity = identity

    def execute(self, statement, params=None):
        return SimpleNamespace(one=lambda: self.identity)


def _fake_engine(identity=("mia_app", False)):
    @contextmanager
    def connect():
        yield _FakeConnection(identity)

    return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=connect)


@pytest.mark.parametrize(
    "raised,expected",
    [
        ("app_role_demote_required", "app_role_demote_required"),
        ("app_role_database_owner_unsafe", "app_role_database_owner_unsafe"),
        ("app_role_membership_unsafe", "app_role_membership_unsafe"),
        ("app_role_ledger_access_unsafe", "app_role_ledger_access_unsafe"),
        ("postgresql://user:secret@host/db", "app_role_check_failed"),
    ],
)
def test_validator_reasons_map_to_fixed_codes(monkeypatch, raised, expected):
    from app.db import app_role_posture as posture

    seen = []

    def validator(conn, role):
        seen.append(role)
        raise ValueError(raised)

    monkeypatch.setattr(posture, "_validate_main_role_catalog", validator)
    assert posture.inspect_posture(_fake_engine()) == expected
    assert seen == ["mia_app"]


def test_passing_validator_and_impersonation(monkeypatch):
    from app.db import app_role_posture as posture

    monkeypatch.setattr(posture, "_validate_main_role_catalog", lambda conn, role: None)
    assert posture.inspect_posture(_fake_engine(("mia_app", False))) == "app_role_ok"
    assert (
        posture.inspect_posture(_fake_engine(("mia_app", True))) == "app_role_impersonation_unsafe"
    )


def test_non_postgres_engine_is_a_fixed_reason():
    from app.db import app_role_posture as posture

    engine = SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))
    assert posture.inspect_posture(engine) == "app_role_postgres_required"
