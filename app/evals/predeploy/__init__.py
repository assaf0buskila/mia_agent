"""Opt-in real-model predeploy eval suite.

Separate from `app.evals.harness`, which is the deterministic suite that runs on every
commit and makes no model calls. This package calls the configured models against the
real website and owner code paths, so it is off unless `MIA_PREDEPLOY_EVAL=1` and real
model credentials are both present.

Entry point: `scripts/run_predeploy_eval.py`.
"""

from app.evals.predeploy.gate import PREDEPLOY_FLAG, GateStatus, gate_status
from app.evals.predeploy.report import (
    EXIT_BLOCKED,
    EXIT_GREEN,
    EXIT_SKIPPED,
    GATE_BLOCKED,
    GATE_GREEN,
    GATE_SKIPPED,
    HARD_SAFETY_RUNS,
    REPORT_VERSION,
    SUITE_NAME,
    AttemptResult,
    ScenarioResult,
    build_report,
    exit_code,
    human_summary,
    pass_at_1_rate,
    pass_hat_3_rate,
    skipped_report,
    write_report,
)
from app.evals.predeploy.sandbox import (
    SEALED_ENV,
    SealBroken,
    assert_sealed,
    seal_process_environment,
    sealed_settings,
)
from app.evals.predeploy.scenarios import (
    hard_safety_ids,
    owner_scenarios,
    scenario_ids,
    website_scenarios,
)

__all__ = [
    "EXIT_BLOCKED",
    "EXIT_GREEN",
    "EXIT_SKIPPED",
    "GATE_BLOCKED",
    "GATE_GREEN",
    "GATE_SKIPPED",
    "HARD_SAFETY_RUNS",
    "PREDEPLOY_FLAG",
    "REPORT_VERSION",
    "SEALED_ENV",
    "SUITE_NAME",
    "AttemptResult",
    "GateStatus",
    "ScenarioResult",
    "SealBroken",
    "assert_sealed",
    "build_report",
    "exit_code",
    "gate_status",
    "hard_safety_ids",
    "human_summary",
    "owner_scenarios",
    "pass_at_1_rate",
    "pass_hat_3_rate",
    "scenario_ids",
    "seal_process_environment",
    "sealed_settings",
    "skipped_report",
    "website_scenarios",
    "write_report",
]
