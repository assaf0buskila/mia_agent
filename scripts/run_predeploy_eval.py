"""Opt-in real-model predeploy gate for Mia. Run this before a release, not in CI.

    MIA_PREDEPLOY_EVAL=1 uv run python scripts/run_predeploy_eval.py --json predeploy.json

What it does: runs 27 scenarios -- 17 on the real website turn loop, 10 on the real owner
agent loop -- against the models the deployment is actually configured with, and prints
Pass@1, Pass^3, the tool path each scenario took, latency, tokens, and every failure.

Why it is opt-in: these are real provider calls and they cost real money, so the suite
refuses to run unless `MIA_PREDEPLOY_EVAL=1` is set *and* model credentials are present.
A default `uv run pytest` never reaches this file.

What it cannot do, by construction (see `app/evals/predeploy/sandbox.py`): touch the
production CRM, send Telegram, send Gmail, create a Calendar event, or make any network
write. Every integration credential is stripped from the settings the scenarios run
with, an in-memory double is injected for every port, the database is forced to in-memory
sqlite, and `assert_sealed` refuses to start if any of that failed.

Exit codes -- a skip is deliberately not a pass:

    0  gate green: every scenario passed, and every hard-safety scenario passed 3/3
    1  blocked: at least one scenario or suite-level check failed
    2  skipped: not opted in, or no callable model. Nothing was proven.
"""

from __future__ import annotations

import argparse

from app.evals.predeploy.report import exit_code, human_summary, write_report
from app.evals.predeploy.runner import run_suite


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_predeploy_eval",
        description="Real-model predeploy gate. Opt-in via MIA_PREDEPLOY_EVAL=1.",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default="",
        help="Write the machine-readable report to this path.",
    )
    parser.add_argument(
        "--only",
        dest="only",
        default="",
        help=(
            "Comma-separated scenario ids to run. Debugging aid: a partial run is "
            "reported honestly and is never a gate for the whole suite."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    only = frozenset(part.strip() for part in args.only.split(",") if part.strip())
    report = run_suite(only=only or None)
    print(human_summary(report))
    if args.json_path:
        write_report(report, args.json_path)
        print(f"\nreport: {args.json_path}")
    return exit_code(report)


if __name__ == "__main__":
    raise SystemExit(main())
