"""Provisioning safety boundaries; all credentials and provider calls are synthetic."""

import importlib.util
from pathlib import Path

import pytest


@pytest.fixture
def provision(monkeypatch):
    path = Path(__file__).resolve().parents[2] / "ops/assaf-os/provision_auth.py"
    spec = importlib.util.spec_from_file_location("assaf_os_provision", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    baseline = {
        "MIA_ENV": "prod", "MIA_PUBLIC_BASE_URL": "https://mia.example.test",
        "MIA_TELEGRAM_BOT_TOKEN": "synthetic-existing-bot",
        "MIA_TELEGRAM_OWNER_USER_IDS": "123456",
        "MIA_UNRELATED_SETTING": "retain-this-value",
    }
    monkeypatch.setattr(module, "settings", lambda: dict(baseline))
    monkeypatch.setattr(module, "verify_vm", lambda: None)
    monkeypatch.setattr(module, "owner_subject", lambda: "12345678901234567890")
    return module, baseline


def test_preflight_never_publishes_or_creates(provision, monkeypatch):
    module, _ = provision
    queries = []
    monkeypatch.setattr(module, "postgres", lambda sql, database="postgres":
                        queries.append((sql, database)) or "")
    monkeypatch.setattr(module, "command", lambda *a, **k: pytest.fail("unexpected mutation"))
    result = module.provision()
    assert result["status"] == "preflight_ready"
    assert result["new_server"] is False and result["features_enabled"] is False
    assert len(queries) == 1 and queries[0][0].startswith("SELECT")


def test_baseline_drift_aborts_before_publication(provision, monkeypatch):
    module, baseline = provision
    reads = iter([dict(baseline), {**baseline, "MIA_UNRELATED_SETTING": "owner-new-value"}])
    monkeypatch.setattr(module, "settings", lambda: next(reads))
    monkeypatch.setattr(module, "postgres", lambda sql, database="postgres": "")
    monkeypatch.setattr(module, "command", lambda *a, **k: pytest.fail("must not publish"))
    with pytest.raises(RuntimeError, match="settings_baseline_changed"):
        module.provision(True)


def test_existing_configuration_never_rotates(provision, monkeypatch):
    module, baseline = provision
    monkeypatch.setattr(module, "settings", lambda:
                        {**baseline, "MIA_ASSAF_OS_HMAC_SECRET": "synthetic-existing"})
    monkeypatch.setattr(module, "postgres", lambda *a, **k: pytest.fail("target must not mutate"))
    with pytest.raises(RuntimeError, match="already_configured_do_not_rotate"):
        module.provision(True)


def test_unknown_database_is_rejected_before_ssh(provision, monkeypatch):
    module, _ = provision
    monkeypatch.setattr(module, "command", lambda *a, **k: pytest.fail("no outbound call"))
    with pytest.raises(RuntimeError, match="database_denied"):
        module.postgres("SELECT 1", "mia")


def test_actual_posture_queries_reject_broad_role(provision, monkeypatch):
    module, _ = provision
    observed = []
    monkeypatch.setattr(module, "postgres", lambda sql, database="postgres":
                        observed.append((sql, database)) or "t|f|f|f|f|f\nt|t|t\nt|t|t\n")
    with pytest.raises(RuntimeError, match="auth_role_posture_failed"):
        module.verify_role_posture()
    assert observed[0][1] == "assaf_os_auth"
    assert "SET LOCAL ROLE assaf_os_session" in observed[0][0]
    assert "has_column_privilege" in observed[0][0]
    assert "pg_auth_members" in observed[0][0]
