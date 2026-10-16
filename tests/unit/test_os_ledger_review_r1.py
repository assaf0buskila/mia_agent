"""Review round 1: execute the migration CI gates and operator refusal paths."""

from types import SimpleNamespace

import pytest
import yaml
from sqlalchemy.exc import SQLAlchemyError

from tests.unit.test_ci_os_ledger_postgres import (
    JOB_KEYS,
    PASSED,
    ROOT,
    SKIPPED_LINE,
    STEP_KEYS,
    _jobs,
    _run_step,
)

JOB = "os-ledger-migration-postgres"
SUITE = "Normal migration isolation and explicit ledger migration contracts"
SUITE_RUN = (
    "rc=0\n"
    "uv run pytest tests/db/test_assaf_os_ledger_migration.py "
    "tests/services/test_os_ledger_role_policy.py -rs "
    "--basetemp=.cache/os-ledger-ci > .cache/os-ledger-ci.log 2>&1 || rc=$?\n"
    "cat .cache/os-ledger-ci.log\n"
    "exit $rc\n"
)
GUARD_RUN = (
    'test -s .cache/os-ledger-ci.log || { echo "::error::no pytest log"; exit 1; }\n'
    "if grep -Eq '^SKIPPED|[0-9]+ skipped' .cache/os-ledger-ci.log; then\n"
    '  echo "::error::tests/db skipped a case while ledger PostgreSQL URLs are set"; exit 1\n'
    "fi\n"
    "grep -Eq '[0-9]+ passed' .cache/os-ledger-ci.log || "
    '{ echo "::error::no passing summary"; exit 1; }\n'
)


def _step(name):
    return next(step for step in _jobs()[JOB]["steps"] if step.get("name") == name)


def test_migration_job_cannot_hide_failures_and_pins_guard():
    job = _jobs()[JOB]
    assert set(job) == JOB_KEYS
    for step in job["steps"]:
        assert set(step) <= STEP_KEYS
        assert ("uses" in step) != ("run" in step)
        assert "uses" in step or "with" not in step
    assert [step for step in job["steps"] if "if" in step] == [
        {"name": "Skip guard", "if": "always()", "run": GUARD_RUN},
    ]
    assert _step(SUITE)["run"] == SUITE_RUN
    labels = [step.get("name") or step.get("run") or step["uses"].split("@")[0]
              for step in job["steps"]]
    assert labels == [
        "actions/checkout", "astral-sh/setup-uv", "uv sync --frozen --group dev",
        "mkdir -p .cache", "Prepare isolated Owner OS posture database", SUITE, "Skip guard",
    ]
    assert job["env"] == {
        "MIA_OS_LEDGER_TEST_POSTGRES_URL":
            "postgresql+psycopg://postgres@localhost:5432/mia_ledger_test",
        "MIA_OWNER_OS_TEST_POSTGRES_URL":
            "postgresql+psycopg://postgres@localhost:5432/owner_os_test",
    }


@pytest.mark.parametrize("rc", [0, 1, 2])
def test_migration_suite_preserves_exit_and_prints_evidence(tmp_path, rc):
    (tmp_path / ".cache").mkdir()
    proc = _run_step(
        _step(SUITE)["run"], tmp_path,
        prelude="uv() { printf '%s' \"$FAKE_OUTPUT\"; return \"$FAKE_RC\"; }\n",
        env={"FAKE_OUTPUT": PASSED, "FAKE_RC": str(rc)},
    )
    assert proc.returncode == rc
    assert proc.stdout == PASSED
    assert (tmp_path / ".cache/os-ledger-ci.log").read_text() == PASSED


@pytest.mark.parametrize("log,ok", [
    (PASSED, True), (None, False), ("", False), (SKIPPED_LINE, False),
    (PASSED + SKIPPED_LINE, False), ("1 passed, 1 skipped", False),
    ("1 skipped", False), ("no tests ran", False), ("2 failed, 1 error", False),
])
def test_migration_skip_guard_executes(tmp_path, log, ok):
    (tmp_path / ".cache").mkdir()
    if log is not None:
        (tmp_path / ".cache/os-ledger-ci.log").write_text(log, newline="\n")
    proc = _run_step(_step("Skip guard")["run"], tmp_path)
    assert (proc.returncode == 0) is ok
    assert ("::error::" not in proc.stdout) is ok


def test_runbook_names_the_migration_gate():
    assert f"CI's `{JOB}` job" in (ROOT / "migrations_assaf_os/README.md").read_text()


def test_container_checks_packaged_migrations_immediately_after_build():
    steps = _jobs()["container"]["steps"]
    build_index = next(i for i, step in enumerate(steps)
                       if step.get("run", "").startswith("docker build "))
    assert "-t mia:ci ." in steps[build_index]["run"]
    assert steps[build_index + 1] == {
        "name": "Ledger migration image smoke",
        "run": "docker run --rm --entrypoint python mia:ci -c "
               "'from app.db.os_ledger_migrate import migration_scripts; migration_scripts()'",
    }


def test_runtime_dsn_wins_over_migration_env_file():
    compose = yaml.safe_load((ROOT / "ops/gcp/vm/compose.yaml").read_text())
    assert compose["services"]["os-ledger-migrate"]["env_file"] == [
        "/etc/mia/os-ledger-migrate.env", "/etc/mia/runtime.env",
    ]


@pytest.mark.parametrize("content,ok", [
    ("MIA_OS_LEDGER_MIGRATION_DATABASE_URL=postgresql://secret\n", True),
    ("# migration only\n\nMIA_OS_LEDGER_MIGRATION_DATABASE_URL='postgresql://secret'\n", True),
    ("MIA_OS_LEDGER_MIGRATION_DATABASE_URL=x\nMIA_DATABASE_URL=secret\n", False),
    ("MIA_OS_LEDGER_MIGRATION_DATABASE_URL=x\nMIA_BUILD_SHA=secret\n", False),
    ("MIA_OS_LEDGER_MIGRATION_DATABASE_URL=x\nexport MIA_DATABASE_URL=secret\n", False),
    ("MIA_OS_LEDGER_MIGRATION_DATABASE_URL='x\nMIA_DATABASE_URL=secret'\n", False),
    ("MIA_OS_LEDGER_MIGRATION_DATABASE_URL='postgresql://priv@127.0.0.1/mia' "
     f"MIA_BUILD_SHA={'b' * 40}\n", False),
    ('MIA_OS_LEDGER_MIGRATION_DATABASE_URL="postgresql://priv@127.0.0.1/mia" '
     f"MIA_BUILD_SHA={'c' * 40}\n", False),
    ("MIA_OS_LEDGER_MIGRATION_DATABASE_URL=postgresql://priv@127.0.0.1/mia "
     f"MIA_BUILD_SHA={'d' * 40}\n", False),
    (None, False),
])
def test_operator_refuses_unexpected_migration_file_keys(tmp_path, content, ok):
    # Execute the actual case arm with only host paths redirected and Compose stubbed.
    # No Docker process, host env file or deployment lock is touched.
    script = (ROOT / "ops/gcp/vm/mia").read_text()
    arm = script.split("\n  os-ledger)\n", 1)[1].split("\n    ;;", 1)[0]
    arm = arm.replace("/etc/mia/os-ledger-migrate.env", "migration.env")
    arm = arm.replace("/opt/mia/tag.env", "tag.fixture")
    (tmp_path / "tag.fixture").touch()
    if content is not None:
        (tmp_path / "migration.env").write_text(content, newline="\n")
    prelude = (
        'set -euo pipefail\n'
        'die() { echo "mia: $*" >&2; exit 1; }\n'
        'compose() { echo compose-called; }\n'
        f'set -- migrate --approved-sha {"a" * 40}\n'
    )
    proc = _run_step(arm, tmp_path, prelude=prelude)
    assert (proc.returncode == 0) is ok, proc.stdout + proc.stderr
    assert ("compose-called" in proc.stdout) is ok
    assert "secret" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("state,expected", [
    ("42501", "ledger_migration_refused:42501\n"),
    ("23514", "ledger_migration_refused:23514\n"),
    ("secret-url", "ledger_migration_refused\n"),
    (None, "ledger_migration_refused\n"),
])
def test_entrypoint_only_emits_valid_sqlstate(monkeypatch, capsys, state, expected):
    from app.workers import os_ledger

    error = SQLAlchemyError("secret provider URL")
    error.orig = SimpleNamespace(sqlstate=state)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setenv("MIA_BUILD_SHA", "a" * 40)
    monkeypatch.setenv("MIA_DATABASE_URL", "main")
    monkeypatch.setenv("MIA_OS_LEDGER_MIGRATION_DATABASE_URL", "privileged")
    monkeypatch.setattr(os_ledger, "create_engine", fail)
    with pytest.raises(SystemExit) as result:
        os_ledger.main(["migrate", "--approved-sha", "a" * 40])
    assert result.value.code == 1
    assert capsys.readouterr().err == expected
