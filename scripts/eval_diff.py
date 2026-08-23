"""Local report of failing eval cases: expected vs actual action and reply.

Not a test. Used to review dataset expectations deliberately after a behavior change
instead of regenerating them blindly.

Run: uv run python scripts/eval_diff.py
"""

from __future__ import annotations

import sys

from app.evals.harness import (
    run_buyer_eval,
    run_extract_eval,
    run_gold_eval,
    run_objection_eval,
    run_safety_eval,
    run_sales_eval,
    run_website_handoff_eval,
    run_writing_eval,
)

RUNNERS = {
    "sales": run_sales_eval,
    "buyer": run_buyer_eval,
    "website_handoff": run_website_handoff_eval,
    "safety": run_safety_eval,
    "objection": run_objection_eval,
    "extract": run_extract_eval,
    "writing": run_writing_eval,
    "gold": run_gold_eval,
}


def main() -> int:
    only = set(sys.argv[1:])
    for name, runner in RUNNERS.items():
        if only and name not in only:
            continue
        report = runner()
        failures = [r for r in report.results if not r.passed]
        print(f"\n=== {name}: {report.failed} failed / {report.passed} passed ===")
        for result in failures:
            print(f"  {result.case_id}")
            print(f"    expected_action={result.expected_action}")
            print(f"    actual_action  ={result.actual_action}")
            print(f"    reply          ={result.reply}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
