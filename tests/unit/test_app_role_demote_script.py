import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def load_tool():
    spec = importlib.util.spec_from_file_location(
        "demote_tool", ROOT / "ops/gcp/vm/app_role_demote.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def tool(monkeypatch):
    module = load_tool()
    # Orchestration uses unprivileged pytest temp files, not root-owned VM files.
    # The production metadata policy is tested independently below.
    monkeypatch.setattr(module, "require_private_metadata", lambda info: None)
    monkeypatch.setattr(module, "authenticate", lambda password: None)
    return module


@pytest.mark.parametrize(
    "uid,mode,safe",
    [
        (0, 0o100600, True),
        (1000, 0o100600, False),
        (0, 0o100644, False),
        (0, 0o100660, False),
        (0, 0o100606, False),
    ],
)
def test_private_metadata_policy(uid, mode, safe):
    from types import SimpleNamespace

    module = load_tool()
    info = SimpleNamespace(st_uid=uid, st_mode=mode)
    if safe:
        module.require_private_metadata(info)
    else:
        with pytest.raises(ValueError, match="app_role_file_unsafe"):
            module.require_private_metadata(info)


def test_stage_activate_enforce_rollback_preserves_other_values(tool, tmp_path):
    runtime = tmp_path / "runtime.env"
    original = "postgresql://mia:old-secret@127.0.0.1:5432/mia"
    runtime.write_text(f"MIA_DATABASE_URL={original}\nOTHER=preserve\n")
    calls = []

    def database(sql):
        calls.append(sql)
        if "SELECT EXISTS" in sql:
            return "f"
        return ""

    tool.perform("stage", tmp_path, database)
    staged = (tmp_path / "app-role.staged.env").read_bytes()
    assert runtime.read_text().startswith(f"MIA_DATABASE_URL={original}")
    assert b"mia_app:" in staged and b"old-secret" not in staged

    def database(sql):
        if sql == tool.CHECK_SQL:
            return json.dumps(
                {
                    "role_exists": True,
                    "role_safe": True,
                    "no_membership": True,
                    "not_database_owner": True,
                    "public_owned": True,
                    "public_create": True,
                    "database_create": False,
                    "ledger_access": False,
                }
            )
        return "t" if "SELECT EXISTS" in sql else ""

    tool.perform("stage", tmp_path, database)
    assert (tmp_path / "app-role.staged.env").read_bytes() == staged
    tool.perform("activate", tmp_path, database)
    assert "mia_app:" in runtime.read_text()
    tool.perform("enforce", tmp_path, database)
    assert "MIA_APP_ROLE_POSTURE=enforce" in runtime.read_text()
    tool.perform("rollback", tmp_path, database)
    assert f"MIA_DATABASE_URL={original}" in runtime.read_text()
    assert "MIA_APP_ROLE_POSTURE=observe" in runtime.read_text()
    assert "OTHER=preserve" in runtime.read_text()
    tool.perform("rollback", tmp_path, database)
    assert f"MIA_DATABASE_URL={original}" in runtime.read_text()
    assert "ALTER ROLE mia_app PASSWORD" in calls[-1]


def test_unmanaged_existing_role_refused_without_staging(tool, tmp_path):
    (tmp_path / "runtime.env").write_text(
        "MIA_DATABASE_URL=postgresql://mia:secret@127.0.0.1:5432/mia\n"
    )
    with pytest.raises(ValueError, match="app_role_unmanaged"):
        tool.perform("stage", tmp_path, lambda sql: "t")
    assert not (tmp_path / "app-role.staged.env").exists()


def test_transport_never_reports_provider_output(tool, monkeypatch):
    from types import SimpleNamespace

    def fail(*args, **kwargs):
        assert "secret" not in repr(args)
        return SimpleNamespace(returncode=1, stdout="secret", stderr="password DSN")

    monkeypatch.setattr(tool.subprocess, "run", fail)
    with pytest.raises(ValueError, match="^app_role_database_failed$"):
        tool.database("SELECT 'secret'")


def test_check_is_read_only_and_only_booleans(tool, tmp_path):
    (tmp_path / "runtime.env").write_text(
        "MIA_DATABASE_URL=postgresql://mia:secret@127.0.0.1:5432/mia\n"
    )
    snapshot = {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    calls = []

    def database(sql):
        calls.append(sql)
        return '{"role_exists":false,"role_safe":false}'

    result = tool.perform("check", tmp_path, database)
    assert all(isinstance(value, bool) for value in result.values())
    assert snapshot == {p.name: p.read_bytes() for p in tmp_path.iterdir()}
    assert len(calls) == 1 and calls[0].lstrip().startswith("SELECT")


def test_rollback_refuses_non_superuser_saved_dsn(tool, tmp_path):
    (tmp_path / "runtime.env").write_text(
        "MIA_DATABASE_URL=postgresql://mia_app:s@127.0.0.1:5432/mia\n"
    )
    (tmp_path / "app-role.rollback.env").write_text(
        "MIA_DATABASE_URL=postgresql://intruder:s@127.0.0.1:5432/mia\n"
    )
    with pytest.raises(ValueError, match="app_role_dsn_invalid"):
        tool.perform("rollback", tmp_path, lambda sql: "")


def test_main_sanitizes_every_failure(tool, monkeypatch, capsys):
    def fail(*args):
        raise RuntimeError("postgresql://user:secret@host/db password=secret")

    monkeypatch.setattr(tool.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(tool, "perform", fail)
    monkeypatch.setattr("sys.argv", ["app_role_demote.py", "--check"])
    assert tool.main() == 1
    captured = capsys.readouterr()
    assert captured.out == "reason=app_role_operation_failed ok=false\n"
    assert captured.err == ""
