"""The PG sentinel (§3.4): a configured PG run must not silently collect fewer cases.

``MIN_PG_CASES`` is the number of conformance cases. The ``checks`` job has no PG, so it generates
no ``pg`` case and these tests only pin that. The ``os-ledger-migration-postgres`` job sets
``MIA_OS_LEDGER_TEST_POSTGRES_URL``; once ``app.services.os_ledger.core`` exists it must collect
every case under ``pg`` as well as ``fake``. Nothing here is ever skipped.
"""

import ast
import os
import re
import subprocess
import sys
from pathlib import Path

from tests.services.os_ledger.conftest import PG_URL_ENV, core_module_present, pg_enabled

MIN_PG_CASES = 407  # 403 on master (V2-16 final) + V2-18's one E14(1) case + V2-16b's 3 (E26)

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]


def test_a_configured_pg_run_collects_at_least_min_pg_cases(case_counts):
    if core_module_present() and os.environ.get(PG_URL_ENV):
        assert case_counts["pg"] >= MIN_PG_CASES, (
            f"collected {case_counts['pg']} pg cases, expected at least {MIN_PG_CASES}")


def test_no_pg_case_exists_unless_both_conditions_hold(case_counts):
    if not pg_enabled():
        assert case_counts["pg"] == 0


def test_the_fake_runs_every_conformance_case_everywhere(case_counts):
    # A partial collection (a -k filter, one file) cannot be judged here, so only a full one counts.
    if case_counts["fake"] >= MIN_PG_CASES:
        assert case_counts["fake"] == MIN_PG_CASES


def test_min_pg_cases_equals_the_conformance_case_count():
    """Collect the conformance directory on its own, whatever this session was narrowed to."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/services/os_ledger/conformance", "--collect-only",
         "-q", "-p", "no:cacheprovider", "-p", "no:warnings", "-o", "addopts="],
        cwd=ROOT, capture_output=True, text=True, timeout=300,
        env={**os.environ, "MIA_ENV": "test", PG_URL_ENV: ""},  # the fake case only
    )
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    fake_cases = re.findall(r"^\S+::\S+\[(?:[^\]]*-)?fake(?:-[^\]]*)?\]$", result.stdout, re.M)
    assert len(fake_cases) == MIN_PG_CASES, (
        f"{len(fake_cases)} conformance cases but MIN_PG_CASES is {MIN_PG_CASES}: update it")


def test_the_pg_fixture_never_calls_provision_ledger():
    tree = ast.parse((HERE / "conftest.py").read_text(encoding="utf-8"))
    called = {
        (node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None))
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    assert "provision_ledger" not in called


def test_the_pg_fixture_imports_role_policy_lazily():
    tree = ast.parse((HERE / "conftest.py").read_text(encoding="utf-8"))
    modules = [n.module or "" for n in tree.body if isinstance(n, ast.ImportFrom)]
    modules += [a.name for n in tree.body if isinstance(n, ast.Import) for a in n.names]
    modules += [a.name for n in tree.body if isinstance(n, ast.ImportFrom) for a in n.names]
    assert not any("role_policy" in module for module in modules)
