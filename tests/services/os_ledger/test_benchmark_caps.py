"""V2-16b review: the §8 benchmark passes only a run made under the load it claims (§22 E23).

``within_caps`` used to compare the two p95 values alone, so a run without the pg_dump loop, with a
loop whose dumps all failed, or on an unlimited container still reported a pass.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "benchmark_assaf_os_ledger.py"


@pytest.fixture(scope="module")
def bench():
    spec = importlib.util.spec_from_file_location("benchmark_assaf_os_ledger", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def result(**changes) -> dict:
    base = {
        "page_p95_ms": 40.0,
        "write_p95_ms": 300.0,
        "pg_limits": "0.5 cpu / 1 GB",
        "concurrent_pg_dump": {"enabled": True, "completed": 12, "failed": 0},
    }
    return base | changes


def test_a_run_under_the_claimed_load_and_within_the_caps_passes(bench):
    assert bench.within_caps(result()) is True


@pytest.mark.parametrize(
    "changes",
    [
        {"concurrent_pg_dump": {"enabled": False, "completed": 0, "failed": 0}},
        {"concurrent_pg_dump": {"enabled": True, "completed": 0, "failed": 7}},
        {"concurrent_pg_dump": {"enabled": True, "completed": 0, "failed": 0}},
        {"concurrent_pg_dump": {"enabled": True, "completed": 9, "failed": 1}},
        {"pg_limits": "0 cpu / 0 GB"},
        {"pg_limits": "unknown"},
        {"pg_limits": "1 cpu / 1 GB"},
    ],
    ids=["no-dump", "dumps-only-failed", "no-dump-completed", "a-dump-failed", "unlimited",
         "limits-unknown", "wrong-cpu"],
)
def test_a_run_without_the_claimed_load_never_passes(bench, changes):
    assert bench.within_caps(result(**changes)) is False


def test_a_run_over_either_cap_fails(bench):
    assert bench.within_caps(result(page_p95_ms=bench.RAISED_CAPS_MS["page_p95"] + 1)) is False
    assert bench.within_caps(result(write_p95_ms=bench.RAISED_CAPS_MS["write_p95"] + 1)) is False
