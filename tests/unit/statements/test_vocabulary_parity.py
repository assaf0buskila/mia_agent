"""The domain copies the expense vocabulary; this proves the copy matches the frozen schema."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.domain.statements import (
    CURRENCIES,
    PATHS,
    PAYMENT_STATUS_CANCELLED,
    PAYMENT_STATUS_PAID,
    PAYMENT_STATUSES,
    PLATFORMS,
    SOURCE_TYPE_ACTUAL_PAYMENT,
    Classification,
    LineFacts,
    map_to_expense,
)
from app.services.assaf_os import INPUT_FIELDS

SCHEMA = Path(__file__).resolve().parents[3] / "app" / "prototypes" / "assaf_os_local_schema.json"


def schema() -> dict:
    return json.loads(SCHEMA.read_text(encoding="utf-8"))


def expense_fields() -> dict[str, dict]:
    return {field["key"]: field for field in schema()["entities"]["expenses"]}


def test_paths_are_the_shared_path_enum():
    assert list(PATHS) == schema()["enums"]["shared_path"]


def test_platforms_are_the_expense_platform_values():
    assert list(PLATFORMS) == expense_fields()["platform"]["values"]


def test_payment_statuses_and_source_type_are_schema_members():
    enums = schema()["enums"]
    assert PAYMENT_STATUS_PAID in enums["payment_status"]
    assert PAYMENT_STATUS_CANCELLED in enums["payment_status"]
    assert SOURCE_TYPE_ACTUAL_PAYMENT in enums["expense_source"]
    assert "ILS" in enums["currency"]


def test_hand_entry_vocabularies_are_the_whole_schema_enums():
    enums = schema()["enums"]
    assert list(PAYMENT_STATUSES) == enums["payment_status"]
    assert list(CURRENCIES) == enums["currency"]


def test_a_mapped_expense_only_uses_the_frozen_input_contract():
    facts = LineFacts(
        txn_date=date(2026, 9, 17),
        merchant_display="DEMO SHOP",
        merchant_key="demo shop",
        amount_minor=1000,
        kind="charge",
        currency="ILS",
    )
    classification = Classification(
        category="x", path="משותף", platform="אחר", is_subscription=False
    )
    draft = map_to_expense(
        "STM_" + "a" * 32, "b" * 64, facts, classification, charge_date=date(2026, 10, 15)
    )
    assert set(draft.as_input()) <= INPUT_FIELDS["expenses"]
    required = {
        field["key"]
        for field in schema()["entities"]["expenses"]
        if field["role"] == "input" and field["required"]
    }
    assert required <= set(draft.as_input())
