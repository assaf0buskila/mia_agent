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


SAFE_STATE = {
    "role_exists": True,
    "role_safe": True,
    "no_membership": True,
    "not_database_owner": True,
    "public_owned": True,
    "public_create": True,
    "database_create": False,
    "ledger_access": False,
}


def reapply_database(tool, calls, state=None, exists="t"):
    def database(sql):
        calls.append(sql)
        if sql == tool.ROLE_EXISTS_SQL:
            return exists
        if sql == tool.CHECK_SQL:
            return json.dumps(SAFE_STATE if state is None else state)
        return ""

    return database


def test_reapply_reruns_the_transfer_and_touches_no_env_file(tool, tmp_path):
    files = {
        "runtime.env": "MIA_DATABASE_URL=postgresql://mia_app:synthetic@127.0.0.1:5432/mia\n",
        "app-role.staged.env": "MIA_DATABASE_URL=postgresql://mia_app:s@127.0.0.1:5432/mia\n",
        "app-role.rollback.env": "MIA_DATABASE_URL=postgresql://mia:old@127.0.0.1:5432/mia\n",
    }
    for name, text in files.items():
        (tmp_path / name).write_text(text)
    calls = []
    result = tool.perform("reapply", tmp_path, reapply_database(tool, calls))
    assert result == {"reapplied": True, "role_exists": True, "ledger_access": False}
    assert {p.name: p.read_text() for p in tmp_path.iterdir()} == files
    assert len(calls) == 3
    assert calls[0] == tool.ROLE_EXISTS_SQL and calls[2] == tool.CHECK_SQL
    script = calls[1]
    assert script.startswith("BEGIN;") and script.rstrip().endswith("COMMIT;")
    assert "SET LOCAL mia.app_role_reapply='on'" in script
    # The same reviewed transfer SQL, with no role creation, password or credential statement.
    assert tool.SQL_PATH.read_text(encoding="utf-8") in script
    assert "ALTER ROLE mia_app PASSWORD" not in script
    assert "PASSWORD '" not in script


def test_reapply_does_not_need_or_read_any_env_file(tool, tmp_path):
    calls = []
    # No runtime.env, staged or rollback file exists at all.
    assert tool.perform("reapply", tmp_path, reapply_database(tool, calls))["reapplied"] is True
    assert list(tmp_path.iterdir()) == []


def test_reapply_is_a_noop_before_the_demote_exists(tool, tmp_path):
    calls = []
    result = tool.perform("reapply", tmp_path, reapply_database(tool, calls, exists="f"))
    assert result == {"reapplied": False, "role_exists": False}
    assert calls == [tool.ROLE_EXISTS_SQL]


def test_reapply_reports_residual_ledger_access_without_failing(tool, tmp_path):
    # A restore resurrects PUBLIC EXECUTE on assaf_os functions. V2-15's reapply clears it.
    state = {**SAFE_STATE, "ledger_access": True}
    result = tool.perform("reapply", tmp_path, reapply_database(tool, [], state))
    assert result == {"reapplied": True, "role_exists": True, "ledger_access": True}


@pytest.mark.parametrize(
    "broken",
    [
        {"role_exists": False},
        {"role_safe": False},
        {"no_membership": False},
        {"not_database_owner": False},
        {"public_owned": False},
        {"public_create": False},
        {"database_create": True},
    ],
)
def test_reapply_fails_closed_when_the_catalog_is_still_unsafe(tool, tmp_path, broken):
    with pytest.raises(ValueError, match="^app_role_check_failed$"):
        tool.perform("reapply", tmp_path, reapply_database(tool, [], {**SAFE_STATE, **broken}))


def test_stage_script_is_unchanged_by_the_reapply_refactor(tool, tmp_path):
    (tmp_path / "runtime.env").write_text(
        "MIA_DATABASE_URL=postgresql://mia:old@127.0.0.1:5432/mia\n"
    )
    calls = []

    def database(sql):
        calls.append(sql)
        return "f" if sql == tool.ROLE_EXISTS_SQL else ""

    tool.perform("stage", tmp_path, database)
    script = calls[-1]
    # The reviewed SQL names the switch; only reapply ever sets it.
    assert "SET LOCAL mia.app_role_reapply" not in script
    assert script.startswith(
        "BEGIN; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='90s';"
    )
    assert tool.SQL_PATH.read_text(encoding="utf-8") in script
    assert script.rstrip().endswith("COMMIT;")


def test_reapply_flag_is_a_mode_and_excludes_the_others(tool, monkeypatch, capsys):
    seen = []

    def fake(mode, directory):
        seen.append((mode, directory))
        return {"reapplied": True}

    monkeypatch.setattr(tool.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(tool, "perform", fake)
    monkeypatch.setattr("sys.argv", ["app_role_demote.py", "--reapply"])
    assert tool.main() == 0
    assert [mode for mode, _ in seen] == ["reapply"]
    assert capsys.readouterr().out == '{"reapplied": true}\n'
    monkeypatch.setattr("sys.argv", ["app_role_demote.py", "--reapply", "--activate"])
    with pytest.raises(SystemExit):
        tool.main()
    assert len(seen) == 1


def test_wrapper_takes_the_state_lock_and_only_forwards_arguments():
    script = (ROOT / "ops/gcp/vm/app-role-demote.sh").read_text(encoding="utf-8")
    assert "\r" not in script
    assert "exec 9>/var/lock/mia-state.lock" in script and "flock -n 9" in script
    assert '"$@"' in script and "app_role_demote.py" in script
    # No container, service, network or secret-store command: it only locks and execs the helper.
    for forbidden in ("docker", "systemctl", "curl", "gcloud", "mia-env", "runtime.env", "cat "):
        assert forbidden not in script


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


STDERR_WITH_SECRETS = (
    "psql: warning: connection to postgresql://mia_app:hunter2@127.0.0.1/mia\n"
    "ERROR:  app_role_extension_object_requires_review\n"
    "CONTEXT:  PL/pgSQL function inline_code_block line 80 at RAISE\n"
    "STATEMENT:  ALTER ROLE mia_app PASSWORD 'hunter2'\n"
)


def test_only_a_fixed_sql_reason_code_is_repeated_from_psql_errors(tool):
    assert tool.sql_reason(STDERR_WITH_SECRETS) == "app_role_extension_object_requires_review"
    generic = "app_role_database_failed"
    for stderr in (
        None,
        "",
        "ERROR:  password authentication failed for user mia_app hunter2\n",
        "ERROR:  app_role_invented_code\n",
        "ERROR:  app_role_extension_object_requires_review for table hunter2\n",
        "NOTICE:  app_role_extension_object_requires_review\n",
    ):
        assert tool.sql_reason(stderr) == generic


def test_transport_repeats_a_sql_reason_but_never_other_provider_output(tool, monkeypatch):
    from types import SimpleNamespace

    def fail(*args, **kwargs):
        return SimpleNamespace(returncode=1, stdout="hunter2", stderr=STDERR_WITH_SECRETS)

    monkeypatch.setattr(tool.subprocess, "run", fail)
    with pytest.raises(ValueError, match="^app_role_extension_object_requires_review$"):
        tool.database("SELECT 1")


@pytest.mark.parametrize(
    "raised,printed",
    [
        (
            ValueError("app_role_extension_object_requires_review"),
            "app_role_extension_object_requires_review",
        ),
        (ValueError("app_role_check_failed"), "app_role_check_failed"),
        (ValueError("postgresql://user:hunter2@host/db"), "app_role_operation_failed"),
        (ValueError("app_role_invented_code"), "app_role_operation_failed"),
        (RuntimeError("app_role_check_failed"), "app_role_operation_failed"),
    ],
)
def test_main_prints_only_fixed_reason_codes(tool, monkeypatch, capsys, raised, printed):
    def fail(*args):
        raise raised

    monkeypatch.setattr(tool.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(tool, "perform", fail)
    monkeypatch.setattr("sys.argv", ["app_role_demote.py", "--reapply"])
    assert tool.main() == 1
    captured = capsys.readouterr()
    assert captured.out == f"reason={printed} ok=false\n"
    assert captured.err == "" and "hunter2" not in captured.out


def test_every_sql_reason_is_raised_by_the_reviewed_sql(tool):
    import re

    raised = set(re.findall(r"RAISE EXCEPTION '([a-z_]+)'", tool.SQL_PATH.read_text("utf-8")))
    assert raised == set(tool.SQL_REASONS)
