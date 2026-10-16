"""Mapping an active business charge line to the frozen 7-entity expense input."""

from __future__ import annotations

from datetime import date

import pytest
from app.domain.statements import (
    Classification,
    LineFacts,
    StatementValidationError,
    is_statement_source_key,
    map_to_expense,
    owns_source_key,
    statement_source_key,
)

SID = "STM_" + "a" * 32
KEY = "b" * 64
CHARGE = date(2026, 10, 15)

# the frozen expense input contract (app.services.assaf_os.INPUT_FIELDS["expenses"])
EXPENSE_INPUT_FIELDS = {
    "date",
    "category",
    "platform",
    "path",
    "vendor",
    "amount_ils",
    "currency",
    "payment_status",
    "source_type",
    "source_key",
    "notes",
}


def facts(**overrides) -> LineFacts:
    base = {
        "txn_date": date(2026, 9, 17),
        "merchant_display": "CLAUDE.AI SUBSCRIPTION",
        "merchant_key": "claude ai subscription",
        "amount_minor": 7500,
        "kind": "charge",
        "currency": "ILS",
        "original_amount_minor": 2000,
        "original_currency": "USD",
    }
    base.update(overrides)
    return LineFacts(**base)


CLASSIFICATION = Classification(
    category="כלי AI", path="משותף", platform="אחר", is_subscription=True
)


def test_maps_to_the_actual_payment_expense_shape():
    draft = map_to_expense(SID, KEY, facts(), CLASSIFICATION, charge_date=CHARGE)
    data = draft.as_input()
    assert set(data) <= EXPENSE_INPUT_FIELDS
    assert data == {
        "date": "2026-10-15",
        "category": "כלי AI",
        "platform": "אחר",
        "path": "משותף",
        "vendor": "CLAUDE.AI SUBSCRIPTION",
        "amount_ils": 7500,
        "currency": "ILS",
        "payment_status": "שולם",
        "source_type": "תשלום בפועל",
        "source_key": f"stmt:{SID}:{KEY}",
    }


def test_foreign_original_amounts_stay_metadata_and_nothing_is_converted():
    data = map_to_expense(SID, KEY, facts(), CLASSIFICATION, charge_date=CHARGE).as_input()
    assert data["amount_ils"] == 7500 and data["currency"] == "ILS"
    assert "original_amount_minor" not in data and "USD" not in data.values()


def test_expense_date_defaults_to_the_charge_date_and_can_be_the_transaction_date():
    default = map_to_expense(SID, KEY, facts(), CLASSIFICATION, charge_date=CHARGE)
    assert default.date == CHARGE
    by_txn = map_to_expense(
        SID, KEY, facts(), CLASSIFICATION, charge_date=CHARGE, date_basis="txn_date"
    )
    assert by_txn.date == date(2026, 9, 17)


def test_unknown_date_basis_is_rejected():
    with pytest.raises(StatementValidationError):
        map_to_expense(SID, KEY, facts(), CLASSIFICATION, charge_date=CHARGE, date_basis="today")  # type: ignore[arg-type]


def test_credits_are_never_posted_as_expenses():
    with pytest.raises(StatementValidationError) as raised:
        map_to_expense(SID, KEY, facts(kind="credit"), CLASSIFICATION, charge_date=CHARGE)
    assert raised.value.issues[0].code == "credit_not_postable"


def test_installment_lines_map_like_any_other_charge():
    draft = map_to_expense(
        SID, KEY, facts(installment_n=2, installment_of=3), CLASSIFICATION, charge_date=CHARGE
    )
    assert draft.amount_ils == 7500


def test_draft_is_immutable():
    draft = map_to_expense(SID, KEY, facts(), CLASSIFICATION, charge_date=CHARGE)
    with pytest.raises(AttributeError):
        draft.amount_ils = 1  # type: ignore[misc]


class TestSourceKeys:
    def test_format(self):
        assert statement_source_key(SID, KEY) == f"stmt:{SID}:{KEY}"

    def test_ownership_is_exact_to_this_statement(self):
        mine = statement_source_key(SID, KEY)
        assert owns_source_key(SID, mine)
        assert not owns_source_key("STM_" + "c" * 32, mine)
        assert not owns_source_key(SID, f"stmt:{SID}:{KEY}extra")
        assert not owns_source_key(SID, f"stmt:{SID}:short")
        assert not owns_source_key(SID, f"stmt:{SID}")
        assert not owns_source_key(SID, "manual:claude-sep")
        assert not owns_source_key(SID, "")

    def test_any_statement_key_is_recognized_so_manual_candidates_can_exclude_them(self):
        assert is_statement_source_key(statement_source_key(SID, KEY))
        assert is_statement_source_key("stmt:anything")
        assert not is_statement_source_key("cash:tip")
        assert not is_statement_source_key("")

    def test_malformed_parts_are_rejected(self):
        with pytest.raises(StatementValidationError):
            statement_source_key("not-a-statement-id", KEY)
        with pytest.raises(StatementValidationError):
            statement_source_key(SID, "XYZ")
