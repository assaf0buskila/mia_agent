"""Offline safety tests; these never contact Google, Docker or PostgreSQL."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "ops/assaf-os/rehearsal/rehearsal.py"
SPEC = importlib.util.spec_from_file_location("rehearsal", SCRIPT)
rehearsal = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rehearsal)


@pytest.fixture
def config():
    return {
        "MIA_DATABASE_URL": "postgresql+psycopg://postgres@127.0.0.1:56217/rehearsal_test",
        "MIA_ASSAF_OS_SCRIPT_URL": "https://script.google.com/macros/s/test-deployment/exec",
        "REHEARSAL_DEPLOYMENT_ID": "test-deployment",
        "MIA_ASSAF_OS_WORKBOOK_ID": "test_workbook_1234567890",
        "MIA_ASSAF_OS_WORKSPACE_ID": "rehearsal-20261016-01",
        "MIA_ASSAF_OS_MODE": "TEST",
        "MIA_ASSAF_OS_HMAC_SECRET": "synthetic-test-secret-not-real-00000000",
        "MIA_TELEGRAM_OWNER_USER_IDS": "123456789",
    }


@pytest.fixture
def pins():
    return {
        "script_urls": ["https://script.google.com/macros/s/production-deployment/exec"],
        "deployment_ids": ["production-deployment"],
        "workbook_ids": ["production_workbook_1234567890"],
    }


def test_valid_rehearsal_passes(config, pins):
    rehearsal.guard(config, pins)


@pytest.mark.parametrize(
    ("changes", "reason"),
    [
        ({"REHEARSAL_DEPLOYMENT_ID": "production-deployment"}, "production_deployment"),
        ({"MIA_ASSAF_OS_SCRIPT_URL": "https://script.google.com/macros/s/production-deployment/exec"},
         "production_script_url"),
        ({"MIA_ASSAF_OS_WORKBOOK_ID": "production_workbook_1234567890"}, "production_workbook"),
        ({"MIA_DATABASE_URL": "postgresql+psycopg://postgres@db.example:56217/rehearsal_test"},
         "dsn_not_loopback"),
        ({"MIA_ASSAF_OS_MODE": "LIVE"}, "mode_not_test"),
        ({"MIA_ASSAF_OS_MODE": "FENCED"}, "mode_not_test"),
        ({"MIA_DATABASE_URL": "postgresql+psycopg://postgres@127.0.0.1:56217/rehearsal_test?host=prod"},
         "dsn_invalid"),
        ({"MIA_DATABASE_URL": "postgresql+psycopg://postgres@localhost:56217/rehearsal_test"},
         "dsn_not_loopback"),
        ({"MIA_ASSAF_OS_SCRIPT_URL": "https://example.com/macros/s/test-deployment/exec"},
         "script_url_invalid"),
        ({"REHEARSAL_DEPLOYMENT_ID": "another-test-deployment"}, "deployment_mismatch"),
        ({"MIA_OWNER_OS_LEDGER_DATABASE_URL": "postgresql+psycopg://ledger@db.example:56217/mia"},
         "dsn_not_loopback"),
        ({"MIA_OWNER_OS_LEDGER_DATABASE_URL": "postgresql+psycopg://ledger@127.0.0.1:5432/mia"},
         "dsn_target_mismatch"),
        ({"MIA_OPENAI_API_KEY": "must-not-be-inherited"}, "config_unknown_key"),
    ],
)
def test_guard_refuses(config, pins, changes, reason):
    config.update(changes)
    with pytest.raises(rehearsal.Refused, match=f"^{reason}$"):
        rehearsal.guard(config, pins)


@pytest.mark.parametrize("key", ["script_urls", "deployment_ids", "workbook_ids"])
def test_missing_pins_fail_closed(config, pins, key):
    pins[key] = []
    with pytest.raises(rehearsal.Refused, match="^pins_incomplete$"):
        rehearsal.guard(config, pins)


def test_url_id_cross_check(config, pins):
    pins["deployment_ids"] = ["test-deployment"]
    with pytest.raises(rehearsal.Refused, match="^production_deployment$"):
        rehearsal.guard(config, pins)


def test_ipv6_loopback_passes(config, pins):
    config["MIA_DATABASE_URL"] = "postgresql+psycopg://postgres@[::1]:56217/rehearsal_test"
    rehearsal.guard(config, pins)


def test_environment_drops_inherited_credentials(config, monkeypatch):
    monkeypatch.setenv("MIA_TELEGRAM_BOT_TOKEN", "sentinel-private")
    monkeypatch.setenv("MIA_OWNER_OS_LEDGER_DATABASE_URL", "sentinel-production")
    monkeypatch.setenv("PGSERVICE", "production")
    monkeypatch.setenv("PYTHONPATH", "sentinel-other-code")
    result = rehearsal.runtime_environment(config, "a" * 40)
    assert "sentinel" not in repr(result)
    assert result["MIA_ENV"] == "test"
    assert result["MIA_BUILD_SHA"] == "a" * 40
    assert result["MIA_ASSAF_OS_ENABLED"] == "true"
    assert result["MIA_AUTOMATION_MODE"] == "off"


@pytest.mark.parametrize(
    ("key", "value", "reason"),
    [
        ("MIA_ASSAF_OS_MODE", "LIVE", "mode_not_test"),
        ("REHEARSAL_DEPLOYMENT_ID", "production-deployment", "production_deployment"),
        ("MIA_DATABASE_URL", "postgresql+psycopg://postgres@db.example:56217/rehearsal_test",
         "dsn_not_loopback"),
    ],
)
def test_invalid_config_aborts_before_any_side_effect(
    config, pins, monkeypatch, capsys, key, value, reason,
):
    config[key] = value
    monkeypatch.setattr(rehearsal, "load_local", lambda: (config, pins))

    def forbidden(*args, **kwargs):
        pytest.fail("guard must run before subprocess or database access")

    monkeypatch.setattr(rehearsal, "command", forbidden)
    monkeypatch.setattr(rehearsal, "check_pg", forbidden)
    assert rehearsal.main(["prepare", "--sha", "a" * 40, "--start-pg"]) == 2
    assert capsys.readouterr().err == f"reason={reason}\n"


def test_command_failure_redacts_output(monkeypatch):
    import subprocess

    def fail(*args, **kwargs):
        return subprocess.CompletedProcess(args, 1, "secret-body", "secret-password")

    monkeypatch.setattr(rehearsal.subprocess, "run", fail)
    with pytest.raises(rehearsal.Refused, match="^git_failed$") as error:
        rehearsal.command(["git", "status"], "git_failed")
    assert "secret" not in str(error.value)


def test_env_parser_rejects_duplicates(tmp_path):
    path = tmp_path / "rehearsal.env"
    path.write_text("MIA_ASSAF_OS_MODE=TEST\nMIA_ASSAF_OS_MODE=LIVE\n", encoding="utf-8")
    with pytest.raises(rehearsal.Refused, match="^config_invalid$"):
        rehearsal.read_config(path)


def test_never_read_dotenv(tmp_path):
    with pytest.raises(rehearsal.Refused, match="^config_path_invalid$"):
        rehearsal.read_config(tmp_path / ".env")


def test_prepare_builds_detached_exact_sha_and_redacts_config(
    config, pins, tmp_path, monkeypatch, capsys,
):
    import subprocess

    sha = "a" * 40
    calls = []
    monkeypatch.setattr(rehearsal, "LOCAL", tmp_path)
    monkeypatch.setattr(rehearsal, "load_local", lambda: (config, pins))
    monkeypatch.setattr(rehearsal, "check_pg", lambda _: calls.append("pg16"))

    def fake_command(args, reason, **kwargs):
        calls.append(args)
        if "symbolic-ref" in args:
            return subprocess.CompletedProcess(args, 1, "", "")
        output = sha if "rev-parse" in args else ""
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(rehearsal, "command", fake_command)
    assert rehearsal.main(["prepare", "--sha", sha]) == 0
    create = next(call for call in calls if isinstance(call, list) and "add" in call)
    assert create == ["git", "-c", "core.autocrlf=false", "worktree", "add", "--detach",
                      str(tmp_path / "worktrees" / sha), sha]
    assert calls.index("pg16") < calls.index(create)
    output = capsys.readouterr().out
    assert "mia_command=" in output and "--sha" in output
    assert "--host 127.0.0.1 --port 58000" in output
    for key in ("MIA_DATABASE_URL", "MIA_ASSAF_OS_HMAC_SECRET", "MIA_ASSAF_OS_SCRIPT_URL"):
        assert config[key] not in output


@pytest.mark.parametrize(
    ("head", "attached", "dirty", "reason"),
    [
        ("b" * 40, False, False, "release_sha_mismatch"),
        ("a" * 40, True, False, "worktree_not_detached"),
        ("a" * 40, False, True, "worktree_not_clean"),
    ],
)
def test_worktree_refuses_wrong_sha_branch_or_dirty(
    tmp_path, monkeypatch, head, attached, dirty, reason,
):
    import subprocess

    def fake_command(args, *_args, **_kwargs):
        if "rev-parse" in args:
            return subprocess.CompletedProcess(args, 0, head, "")
        if "symbolic-ref" in args:
            return subprocess.CompletedProcess(args, 0 if attached else 1, "", "")
        return subprocess.CompletedProcess(args, 0, " M app/main.py" if dirty else "", "")

    monkeypatch.setattr(rehearsal, "command", fake_command)
    with pytest.raises(rehearsal.Refused, match=f"^{reason}$"):
        rehearsal.verify_worktree(tmp_path, "a" * 40)


def test_pg16_probe_and_no_environment_redirection(config, monkeypatch):
    import psycopg

    monkeypatch.setenv("PGSERVICE", "must-not-be-used")
    monkeypatch.setenv("PGHOSTADDR", "192.0.2.1")
    calls = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            pass

        def cursor(self):
            return self

        def execute(self, sql):
            calls.append(sql)

        def fetchone(self):
            return ("160006",)

    def connect(**kwargs):
        assert kwargs["host"] == "127.0.0.1"
        assert kwargs["port"] == 56217
        assert kwargs["dbname"] == "rehearsal_test"
        assert "PGSERVICE" not in rehearsal.os.environ
        assert "PGHOSTADDR" not in rehearsal.os.environ
        return Connection()

    monkeypatch.setattr(psycopg, "connect", connect)
    rehearsal.check_pg(config)
    assert calls == ["SHOW server_version_num"]
    monkeypatch.setattr(Connection, "fetchone", lambda _: ("150012",))
    with pytest.raises(rehearsal.Refused, match="^pg_version_not_16$"):
        rehearsal.check_pg(config)


def test_pg_failure_redacts_provider_message(config, monkeypatch):
    import psycopg

    def fail(**kwargs):
        raise psycopg.OperationalError("secret provider connection string")

    monkeypatch.setattr(psycopg, "connect", fail)
    with pytest.raises(rehearsal.Refused, match="^pg_unavailable$"):
        rehearsal.check_pg(config)


def test_docker_start_is_local_disposable_and_without_pull(config, monkeypatch):
    import json
    import subprocess

    calls = []
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.setattr(rehearsal, "check_pg", lambda _: None)

    def fake_command(args, *_args, **_kwargs):
        calls.append(args)
        output = json.dumps([{"Endpoints": {"docker": {"Host": "npipe:////./pipe/docker_engine"}}}])
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(rehearsal, "command", fake_command)
    rehearsal.start_pg(config, "a" * 40)
    start = calls[1]
    assert "127.0.0.1:56217:5432" in start
    assert all(value in start for value in ("--rm", "--tmpfs", "--pull=never", "postgres:16"))
    assert "--volume" not in start


def test_remote_docker_context_refused(config, monkeypatch):
    import json
    import subprocess

    monkeypatch.delenv("DOCKER_HOST", raising=False)

    def fake_command(args, *_args, **_kwargs):
        assert args == ["docker", "context", "inspect"]
        output = json.dumps([{"Endpoints": {"docker": {"Host": "ssh://production"}}}])
        return subprocess.CompletedProcess(args, 0, output, "")

    monkeypatch.setattr(rehearsal, "command", fake_command)
    with pytest.raises(rehearsal.Refused, match="^docker_not_local$"):
        rehearsal.start_pg(config, "a" * 40)
