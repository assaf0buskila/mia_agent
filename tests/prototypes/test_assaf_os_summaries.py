"""Parity and edge-case tests for the local Assaf OS summaries."""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest
from app.prototypes.assaf_os_summaries import MAX_SAFE_INTEGER, SummaryInputError, build_summaries

ROOT = Path(__file__).resolve().parents[2]
ORACLE = Path(__file__).with_name("assaf_os_summary_oracle.cjs")


def row(entity: str, record_id: str, **values):
    id_keys = {
        "leads": "lead_id",
        "clients": "person_id",
        "works": "work_id",
        "payments": "payment_id",
        "time": "time_id",
        "expenses": "expense_id",
        "tasks": "task_id",
    }
    return {id_keys[entity]: record_id, "revision": 1, **values}


def fixture_records():
    return {
        "leads": [],
        "clients": [row("clients", "PER_1", name="Dana")],
        "works": [
            row(
                "works",
                "WOR_1",
                person_id="PER_1",
                path="לימוד",
                currency="ILS",
                agreed_amount_ils=999_999,
            ),
            row("works", "WOR_2", person_id="PER_1", path="אוטומציה", currency="ILS"),
            row("works", "WOR_3", person_id="PER_1", path="אוטומציה", currency="EUR"),
        ],
        "payments": [
            row(
                "payments",
                "PAY_1",
                person_id="PER_1",
                work_id="WOR_1",
                path="לימוד",
                date="2026-10-01",
                actual_amount_ils=10_001,
                currency="ILS",
                kind="התקבל",
            ),
            row(
                "payments",
                "PAY_2",
                person_id="PER_1",
                work_id="WOR_2",
                path="אוטומציה",
                date="2026-10-02",
                actual_amount_ils=5_000,
                currency="ILS",
                kind="התקבל",
            ),
            row(
                "payments",
                "PAY_3",
                person_id="PER_1",
                work_id="WOR_3",
                path="אוטומציה",
                date="2026-10-02",
                actual_amount_ils=7_000,
                currency="EUR",
                kind="התקבל",
            ),
            row(
                "payments",
                "PAY_4",
                person_id="PER_1",
                work_id="WOR_1",
                path="לימוד",
                date="2026-10-03",
                actual_amount_ils=1_001,
                currency="ILS",
                kind="החזר",
                original_payment_id="PAY_1",
            ),
            row(
                "payments",
                "PAY_5",
                person_id="PER_1",
                work_id="WOR_1",
                path="לימוד",
                date="2026-11-01",
                actual_amount_ils=90_000,
                currency="ILS",
                kind="בוטל",
            ),
        ],
        "time": [
            row(
                "time",
                "TIM_1",
                person_id="PER_1",
                work_id="WOR_1",
                path="לימוד",
                date="2026-10-01",
                minutes=144,
                status="פעיל",
            ),
            row(
                "time",
                "TIM_2",
                person_id="PER_1",
                work_id="WOR_2",
                path="אוטומציה",
                date="2026-10-02",
                minutes=60,
                status="בוטל",
            ),
            row(
                "time",
                "TIM_3",
                person_id="PER_1",
                work_id="WOR_3",
                path="אוטומציה",
                date="2026-10-02",
                minutes=60,
                status="פעיל",
            ),
        ],
        "expenses": [
            row(
                "expenses",
                "EXP_1",
                date="2026-10-02",
                amount_ils=2_000,
                currency="ILS",
                payment_status="שולם",
                source_type="תשלום בפועל",
            ),
            row(
                "expenses",
                "EXP_2",
                date="2026-10-02",
                amount_ils=8_000,
                currency="ILS",
                payment_status="לא שולם",
                source_type="תשלום בפועל",
            ),
            row(
                "expenses",
                "EXP_3",
                date="2026-10-02",
                amount_ils=4_000,
                currency="EUR",
                payment_status="שולם",
                source_type="דיווח שימוש בפלטפורמה",
            ),
        ],
        "tasks": [],
    }


def oracle(records, coverage, aggregate="complete"):
    completed = subprocess.run(
        ["node", str(ORACLE)],
        input=json.dumps(
            {"records": records, "coverage": coverage, "aggregate_coverage": aggregate},
            ensure_ascii=False,
        ),
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
        cwd=ROOT,
    )
    return json.loads(completed.stdout)


def test_monthly_matches_actual_apps_script_vm_and_projections_are_independent():
    records = fixture_records()
    coverage = dict.fromkeys(records, "complete")
    result = build_summaries(records, coverage)
    actual = oracle(records, coverage)
    assert {"coverage": result["coverage"], "monthly": result["monthly"]} == actual

    october_ils = next(
        item
        for item in result["monthly"]
        if item["month"] == "2026-10" and item["currency"] == "ILS"
    )
    assert october_ils == {
        "month": "2026-10",
        "currency": "ILS",
        "paid_income_ils": 14_000,
        "paid_expenses_ils": 2_000,
        "net_cash_ils": 12_000,
        "recorded_minutes": 144,
        "effective_hourly_ils": None,
        "coverage": "partial",
        "reason_codes": ["missing_time"],
    }
    assert result["works"][0]["received_ils"] == 9_000
    assert result["works"][0]["hourly_ils"] == 3_750
    assert result["works"][1]["reason_codes"] == ["missing_time"]
    assert result["works"][2]["received_ils"] is None
    assert result["clients"][0]["total_received_ils"] == 14_000
    assert result["clients"][0]["hourly_ils"] is None
    assert result["clients"][0]["foreign_currency_flag"] is True
    assert all(item["paid_income_ils"] != 999_999 for item in result["monthly"])


@pytest.mark.parametrize(
    "entity,value",
    [("payments", "partial"), ("expenses", "unknown"), ("time", "partial"), ("works", "unknown")],
)
def test_incomplete_coverage_matches_apps_script_nulls(entity, value):
    records = fixture_records()
    coverage = dict.fromkeys(records, "complete")
    coverage[entity] = value
    result = build_summaries(records, coverage, "partial")
    actual = oracle(records, coverage, "partial")
    assert {"coverage": result["coverage"], "monthly": result["monthly"]} == actual
    assert result["coverage"] == "partial"
    assert any(item["coverage"] == "partial" for item in result["monthly"])


def test_unknown_aggregate_and_entity_coverage_match_apps_script():
    records = fixture_records()
    coverage = dict.fromkeys(records, "unknown")
    result = build_summaries(records, coverage, "unknown")
    actual = oracle(records, coverage, "unknown")
    assert {"coverage": result["coverage"], "monthly": result["monthly"]} == actual
    assert all(
        item["paid_income_ils"] is None
        and item["paid_expenses_ils"] is None
        and item["recorded_minutes"] is None
        for item in result["monthly"]
    )
    assert all(item["coverage"] == "partial" for item in result["monthly"])


@pytest.mark.parametrize("currency", [pytest.param(None, id="null"), pytest.param("omitted")])
def test_time_on_work_with_falsy_optional_currency_defaults_to_ils_like_apps_script(currency):
    records = {
        entity: []
        for entity in ("leads", "clients", "works", "payments", "time", "expenses", "tasks")
    }
    work = row("works", "WOR_NO_CURRENCY", person_id="PER_1", path="לימוד")
    if currency is None:
        work["currency"] = None
    records["works"] = [work]
    records["time"] = [
        row(
            "time",
            "TIM_NO_CURRENCY",
            person_id="PER_1",
            work_id="WOR_NO_CURRENCY",
            path="לימוד",
            date="2026-10-04",
            minutes=30,
            status="פעיל",
        )
    ]
    coverage = dict.fromkeys(records, "complete")
    result = build_summaries(records, coverage)
    actual = oracle(records, coverage)
    assert {"coverage": result["coverage"], "monthly": result["monthly"]} == actual
    assert result["monthly"][0]["currency"] == "ILS"
    assert result["monthly"][0]["recorded_minutes"] == 30


def test_totals_keep_currencies_separate_and_propagate_missing_time():
    result = build_summaries(fixture_records())
    assert result["totals"] == [
        {
            "currency": "EUR",
            "paid_income_ils": 7_000,
            "paid_expenses_ils": 0,
            "net_cash_ils": 7_000,
            "recorded_minutes": 60,
            "effective_hourly_ils": 7_000,
            "coverage": "complete",
            "reason_codes": [],
        },
        {
            "currency": "ILS",
            "paid_income_ils": 14_000,
            "paid_expenses_ils": 2_000,
            "net_cash_ils": 12_000,
            "recorded_minutes": 144,
            "effective_hourly_ils": None,
            "coverage": "partial",
            "reason_codes": ["missing_time"],
        },
    ]


def test_safe_half_up_rounding_does_not_use_bankers_or_float_math():
    records = fixture_records()
    records["payments"] = [
        row(
            "payments",
            "PAY_TIE",
            person_id="PER_1",
            work_id="WOR_1",
            path="לימוד",
            date="2026-10-01",
            actual_amount_ils=1,
            currency="ILS",
            kind="התקבל",
        )
    ]
    records["time"] = [
        row(
            "time",
            "TIM_TIE",
            person_id="PER_1",
            work_id="WOR_1",
            path="לימוד",
            date="2026-10-01",
            minutes=24,
            status="פעיל",
        )
    ]
    records["expenses"] = []
    result = build_summaries(records)
    assert result["monthly"][0]["effective_hourly_ils"] == 3
    assert result["works"][0]["hourly_ils"] == 3


def test_input_is_not_mutated_and_checked_integer_contract_fails_closed():
    records = fixture_records()
    before = deepcopy(records)
    build_summaries(records)
    assert records == before

    records["payments"][0]["actual_amount_ils"] = 10.5
    with pytest.raises(SummaryInputError, match="invalid_payment_amount"):
        build_summaries(records)

    records = fixture_records()
    records["payments"][0]["actual_amount_ils"] = MAX_SAFE_INTEGER
    with pytest.raises(SummaryInputError, match="amount_overflow"):
        build_summaries(records)

    records = fixture_records()
    records["payments"][-1]["actual_amount_ils"] = False
    with pytest.raises(SummaryInputError, match="invalid_payment_amount"):
        build_summaries(records)


def test_stable_ids_revision_and_shape_are_required():
    records = fixture_records()
    records["clients"][0]["revision"] = True
    with pytest.raises(SummaryInputError, match="invalid_revision"):
        build_summaries(records)
    records = fixture_records()
    records["works"].append(deepcopy(records["works"][0]))
    with pytest.raises(SummaryInputError, match="invalid_or_duplicate_record_id"):
        build_summaries(records)
