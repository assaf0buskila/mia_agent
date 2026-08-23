from pathlib import Path

from app.evals.harness import (
    GOLD_DATASET_VERSION,
    GoldCase,
    load_gold_dataset,
    run_gold_eval,
)

_GOLD_DATASET_PATH = (
    Path(__file__).resolve().parents[2]
    / "app"
    / "evals"
    / "datasets"
    / "mia_sales_gold.jsonl"
)


def test_gold_dataset_has_twenty_unique_cases_with_hidden_truth() -> None:
    cases = load_gold_dataset()
    # A floor, not a fixed size. Coverage may grow; it must never shrink.
    assert len(cases) >= 20
    case_ids = {case.case_id for case in cases}
    assert len(case_ids) == len(cases)
    for case in cases:
        assert case.hidden_truth is not None


def test_run_gold_eval_all_pass() -> None:
    report = run_gold_eval()
    assert report.dataset_version == GOLD_DATASET_VERSION
    assert report.failed == 0
    assert report.passed == len(load_gold_dataset())
    assert all(result.passed for result in report.results)


def test_mutated_gold_expected_action_fails() -> None:
    failing = GoldCase(
        case_id="synthetic_fail",
        sales={},
        expected_action="disqualify",
        expected_reply_contains="יום רגיל בעסק",
        hidden_truth={"must_ask_workflow": True},
    )
    report = run_gold_eval(cases=[failing])
    assert report.failed == 1
    assert report.results[0].passed is False


def test_gold_dataset_has_no_pii_markers() -> None:
    raw = _GOLD_DATASET_PATH.read_text(encoding="utf-8")
    assert "@" not in raw
    assert "972" not in raw
