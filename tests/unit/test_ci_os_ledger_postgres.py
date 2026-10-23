"""V2-13: CI runs every tests/prototypes PostgreSQL case, and a skipped case turns the job red.

The ledger prototype tests skip themselves when MIA_OS_LOCAL_PROTOTYPE_URL is unset, which is how
the ``checks`` job sees them. The ``os-ledger-postgres`` job sets the URL, so a skip there would
mean the evidence silently stopped running. These tests pin the workflow text, and run the job's
own shell steps against fixture pytest output, so the guard is proven by behaviour and not by grep.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from app.prototypes.assaf_os_ledger import LocalLedgerPrimary
from app.prototypes.assaf_os_primary import LocalBinding
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
CI = ROOT / ".github" / "workflows" / "ci.yml"
JOB = "os-ledger-postgres"
DATABASE = "assaf_os_local_ci"
URL = f"postgresql+psycopg://postgres@127.0.0.1:5432/{DATABASE}"
MARKER_SQL = f"ALTER DATABASE {DATABASE} SET assaf_os.local_prototype = 'disposable_only'"
SUITE_STEP = "Prototype ledger suite on PostgreSQL 16"
MARKER_STEP = "Mark the database disposable"
GUARD_STEP = "Skip guard"

# Every key the job and its steps may carry. Anything else, such as continue-on-error, a step
# env or an extra `if`, can leave the skip guard green over a failing or skipped suite.
JOB_KEYS = {"runs-on", "timeout-minutes", "services", "env", "steps"}
STEP_KEYS = {"name", "run", "uses", "with", "if"}

# The suite and guard commands exactly as frozen in V2_WAVE2_CONTRACTS.md section 5. A block
# scalar keeps one trailing newline, so each constant ends with "\n".
SUITE_RUN = (
    "rc=0\n"
    "uv run pytest tests/prototypes -rs --basetemp=.cache/os-ledger-ci "
    "> .cache/os-ledger.log 2>&1 || rc=$?\n"
    "cat .cache/os-ledger.log\n"
    "exit $rc\n"
)
GUARD_RUN = (
    'test -s .cache/os-ledger.log || { echo "::error::no pytest log"; exit 1; }\n'
    "if grep -Eq '^SKIPPED|[0-9]+ skipped' .cache/os-ledger.log; then\n"
    '  echo "::error::tests/prototypes skipped a case '
    'while MIA_OS_LOCAL_PROTOTYPE_URL is set"; exit 1\n'
    "fi\n"
    "grep -Eq '[0-9]+ passed' .cache/os-ledger.log || "
    '{ echo "::error::no passing summary"; exit 1; }\n'
)

# Real pytest -rs output shapes: the "SKIPPED" short-summary line and the final summary line.
PASSED = "........ [100%]\n238 passed in 20.96s\n"
SKIPPED_LINE = (
    "SKIPPED [1] tests/prototypes/test_assaf_os_ledger.py:44: "
    "explicit disposable MIA_OS_LOCAL_PROTOTYPE_URL required\n"
)


def _jobs() -> dict:
    return yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"]


def _job() -> dict:
    jobs = _jobs()
    assert JOB in jobs, f"{JOB} is missing from .github/workflows/ci.yml"
    return jobs[JOB]


def _step(name: str) -> dict:
    matches = [step for step in _job()["steps"] if step.get("name") == name]
    assert len(matches) == 1, f"expected exactly one step named {name!r}"
    return matches[0]


def _bash() -> str:
    # On Windows, PATH can resolve "bash" to the WSL launcher, which cannot see these paths.
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramW6432")):
            candidate = Path(base or "") / "Git" / "bin" / "bash.exe"
            if base and candidate.is_file():
                return str(candidate)
    found = shutil.which("bash")
    assert found, "bash is required to run the CI step scripts"
    return found


def _run_step(
    script: str, cwd: Path, *, prelude: str = "", env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    # GitHub runs an unspecified-shell step as `bash -e {0}`, a script file, with LF endings.
    path = cwd / "step.sh"
    path.write_text(prelude + script, encoding="utf-8", newline="\n")
    return subprocess.run(
        [_bash(), "-e", "step.sh"],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=60,
    )


def test_job_matches_the_frozen_contract() -> None:
    job = _job()
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == 15
    postgres = job["services"]["postgres"]
    assert postgres["image"] == "postgres:16-alpine"
    assert postgres["env"] == {
        "POSTGRES_HOST_AUTH_METHOD": "trust",
        "POSTGRES_DB": DATABASE,
    }
    assert postgres["ports"] == ["5432:5432"]
    health = postgres["options"]
    assert f'--health-cmd "pg_isready -U postgres -d {DATABASE}"' in health
    for flag in ("--health-interval 5s", "--health-timeout 5s", "--health-retries 10"):
        assert flag in health
    assert job["env"] == {"MIA_OS_LOCAL_PROTOTYPE_URL": URL}


def test_job_and_steps_carry_no_key_that_could_hide_a_failure() -> None:
    # Pinning chosen keys and the two run texts leaves room for `continue-on-error` on the job or
    # on the suite step, or an `if: false`: the guard would then see a log with no skip and a
    # passing summary, and the job would go green over failing ledger tests.
    job = _job()
    assert set(job) == JOB_KEYS
    steps = job["steps"]
    for step in steps:
        assert set(step) <= STEP_KEYS, f"unexpected step keys {sorted(set(step) - STEP_KEYS)}"
        assert ("uses" in step) != ("run" in step), "a step runs a command or uses an action"
        assert "uses" in step or "with" not in step
    assert [step["name"] for step in steps if "if" in step] == [GUARD_STEP]
    assert [step["if"] for step in steps if "if" in step] == ["always()"]
    # The whole sequence is pinned, so an inserted or reordered step is a deliberate test change.
    labels = [step.get("name") or step.get("run") or step["uses"].split("@")[0] for step in steps]
    assert labels == [
        "actions/checkout",
        "astral-sh/setup-uv",
        "uv sync --frozen --group dev",
        "mkdir -p .cache",
        MARKER_STEP,
        SUITE_STEP,
        GUARD_STEP,
    ]


def test_suite_step_is_the_frozen_contract_command_exactly() -> None:
    # Fragment checks are not enough. Appending -k, --deselect or a narrower path still passes
    # them, and the skip guard stays green too, because a deselected case is not a skipped one.
    assert _step(SUITE_STEP)["run"] == SUITE_RUN


def test_guard_step_is_the_frozen_contract_command_exactly() -> None:
    # The guard regex is frozen. Widening it to cover "deselected" would be a contract change.
    assert _step(GUARD_STEP)["run"] == GUARD_RUN


def test_ci_url_passes_the_real_disposable_ledger_guard() -> None:
    url = _job()["env"]["MIA_OS_LOCAL_PROTOTYPE_URL"]
    parsed = make_url(url)
    assert (parsed.host, parsed.database, dict(parsed.query)) == ("127.0.0.1", DATABASE, {})
    engine = create_engine(url)  # lazy: this never connects
    try:
        LocalLedgerPrimary(engine, LocalBinding("guard", "12345"), enabled=True)._guard()
    finally:
        engine.dispose()


def test_one_database_name_is_used_by_the_service_url_health_check_and_marker() -> None:
    job = _job()
    health_db = job["services"]["postgres"]["options"].split("-d ")[1].split('"')[0]
    names = {
        job["services"]["postgres"]["env"]["POSTGRES_DB"],
        make_url(job["env"]["MIA_OS_LOCAL_PROTOTYPE_URL"]).database,
        health_db,
    }
    assert names == {DATABASE}
    marker = _step(MARKER_STEP)["run"]
    assert MARKER_SQL in marker
    assert f"dbname={DATABASE}" in marker


def test_steps_pin_the_same_actions_as_owner_os_postgres() -> None:
    expected = [step for step in _jobs()["owner-os-postgres"]["steps"] if "uses" in step]
    actual = [step for step in _job()["steps"] if "uses" in step]
    assert actual == expected
    assert [step["uses"].split("@")[0] for step in actual] == [
        "actions/checkout",
        "astral-sh/setup-uv",
    ]


def test_steps_run_in_order_and_the_guard_always_runs_last() -> None:
    steps = _job()["steps"]
    runs = [step.get("run", "") for step in steps]

    def index(needle: str) -> int:
        hits = [i for i, run in enumerate(runs) if needle in run]
        assert len(hits) == 1, f"expected one step containing {needle!r}"
        return hits[0]

    sync, cache = index("uv sync --frozen --group dev"), index("mkdir -p .cache")
    marker, suite = index(MARKER_SQL), index("pytest tests/prototypes")
    assert sync < cache < marker < suite
    assert steps[-1]["name"] == GUARD_STEP
    assert steps[-1]["if"] == "always()"
    assert steps[suite]["name"] == SUITE_STEP
    assert "-rs" in runs[suite]
    assert ".cache/os-ledger.log" in runs[suite]


def test_marker_step_feeds_uv_a_script_that_marks_the_ci_database(tmp_path: Path) -> None:
    # A stand-in `uv` records its arguments and stdin, so the heredoc itself is what is tested.
    prelude = "uv() { printf '%s' \"$*\" > uv-args.txt; cat > uv-stdin.py; }\n"
    proc = _run_step(_step(MARKER_STEP)["run"], tmp_path, prelude=prelude)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "here-document" not in proc.stderr
    assert (tmp_path / "uv-args.txt").read_text(encoding="utf-8") == "run python -"
    tree = ast.parse((tmp_path / "uv-stdin.py").read_text(encoding="utf-8"))
    strings = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert MARKER_SQL in strings
    assert f"host=127.0.0.1 port=5432 user=postgres dbname={DATABASE}" in strings
    imports = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert imports == {"psycopg"}


def test_container_waits_for_the_ledger_job() -> None:
    needs = _jobs()["container"]["needs"]
    assert set(needs) == {
        "checks",
        "postgres",
        "quick-leads-postgres",
        "owner-os-postgres",
        JOB,
        "os-ledger-migration-postgres",
        "os-cutover-postgres",
    }
    assert len(needs) == len(set(needs))


@pytest.mark.parametrize("pytest_rc", [0, 1, 2])
def test_suite_step_keeps_pytest_exit_code_and_prints_the_log(
    tmp_path: Path, pytest_rc: int
) -> None:
    (tmp_path / ".cache").mkdir()
    prelude = (
        "uv() { printf '%s\\n' \"$*\" > uv-args.txt; printf '%s' \"$FAKE_PYTEST_OUTPUT\"; "
        'return "$FAKE_PYTEST_RC"; }\n'
    )
    proc = _run_step(
        _step(SUITE_STEP)["run"],
        tmp_path,
        prelude=prelude,
        env={"FAKE_PYTEST_OUTPUT": PASSED, "FAKE_PYTEST_RC": str(pytest_rc)},
    )
    assert proc.returncode == pytest_rc, proc.stdout + proc.stderr
    assert "238 passed" in proc.stdout
    assert "238 passed" in (tmp_path / ".cache" / "os-ledger.log").read_text(encoding="utf-8")
    args = (tmp_path / "uv-args.txt").read_text(encoding="utf-8")
    assert args.startswith("run pytest tests/prototypes -rs")


def _guard(tmp_path: Path, log: str | None) -> subprocess.CompletedProcess[str]:
    (tmp_path / ".cache").mkdir(exist_ok=True)
    if log is not None:
        (tmp_path / ".cache" / "os-ledger.log").write_text(log, encoding="utf-8", newline="\n")
    return _run_step(_step(GUARD_STEP)["run"], tmp_path)


def test_skip_guard_accepts_a_run_with_no_skips(tmp_path: Path) -> None:
    proc = _guard(tmp_path, PASSED)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "::error::" not in proc.stdout


@pytest.mark.parametrize(
    ("log", "message"),
    [
        (None, "no pytest log"),
        ("", "no pytest log"),
        (PASSED + "\n" + SKIPPED_LINE + "237 passed, 1 skipped in 20s\n", "skipped a case"),
        (SKIPPED_LINE, "skipped a case"),
        ("136 passed, 102 skipped in 4.99s\n", "skipped a case"),
        ("1 skipped in 0.1s\n", "skipped a case"),
        ("no tests ran in 0.01s\n", "no passing summary"),
        ("2 failed, 1 error in 3s\n", "no passing summary"),
    ],
    ids=[
        "missing-log",
        "empty-log",
        "skipped-line-and-summary",
        "skipped-line-only",
        "skipped-summary-only-with-passes",
        "skipped-summary-only",
        "nothing-collected",
        "no-pass-at-all",
    ],
)
def test_skip_guard_fails_when_anything_is_skipped_or_nothing_passed(
    tmp_path: Path, log: str | None, message: str
) -> None:
    proc = _guard(tmp_path, log)
    assert proc.returncode != 0
    assert "::error::" in proc.stdout
    assert message in proc.stdout
