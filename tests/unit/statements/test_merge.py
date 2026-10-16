"""Matching a statement line to hand-entered expenses so a charge shows once."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from app.domain.statements import (
    LineFacts,
    ManualExpense,
    MergeItem,
    StatementValidationError,
    match_batch,
    match_manual,
    vendor_matches,
)

from tests.unit.statements.helpers import fixture_manual_expenses, manual_expense

CHARGE = date(2026, 10, 15)


def facts(**overrides) -> LineFacts:
    base = {
        "txn_date": date(2026, 9, 17),
        "merchant_display": "CLAUDE.AI SUBSCRIPTION",
        "merchant_key": "claude ai subscription",
        "amount_minor": 7500,
        "kind": "charge",
        "currency": "ILS",
    }
    base.update(overrides)
    return LineFacts(**base)


def expense(**overrides):
    base = {
        "expense_id": "e1",
        "source_key": "manual:one",
        "date": "2026-09-17",
        "vendor": "Claude AI Subscription",
        "amount_minor": 7500,
        "currency": "ILS",
        "payment_status": "שולם",
    }
    base.update(overrides)
    return manual_expense(**base)


class TestVendorMatching:
    def test_equal_keys_match(self):
        assert vendor_matches("Claude  AI Subscription", "claude ai subscription")

    def test_a_shorter_whole_word_prefix_matches_either_way(self):
        assert vendor_matches("Claude", "claude ai subscription")
        assert vendor_matches("Claude AI Subscription Pro", "claude ai subscription")

    def test_partial_words_and_tiny_vendors_do_not_match(self):
        assert not vendor_matches("Clau", "claude ai subscription")
        assert not vendor_matches("cl", "cl ai")
        assert not vendor_matches("", "claude")

    def test_aliases_extend_the_match(self):
        assert not vendor_matches("Anthropic", "claude ai subscription")
        assert vendor_matches("Anthropic", "claude ai subscription", ("anthropic",))


class TestSingleLine:
    def test_exactly_one_candidate_is_a_proposed_link(self):
        match = match_manual(facts(), [expense()], charge_date=CHARGE)
        assert (match.outcome, match.expense_id, match.candidate_ids) == ("one", "e1", ("e1",))

    def test_no_candidate(self):
        match = match_manual(facts(), [], charge_date=CHARGE)
        assert (match.outcome, match.expense_id, match.candidate_ids) == ("none", None, ())

    def test_several_candidates_must_be_chosen_and_nothing_merges(self):
        match = match_manual(
            facts(), [expense(expense_id="e2"), expense(expense_id="e1")], charge_date=CHARGE
        )
        assert match.outcome == "choose" and match.expense_id is None
        assert match.candidate_ids == ("e1", "e2")

    def test_ils_amount_or_original_amount_matches(self):
        usd = expense(amount_minor=2000, currency="USD")
        with_original = facts(original_amount_minor=2000, original_currency="USD")
        assert match_manual(with_original, [usd], charge_date=CHARGE).outcome == "one"
        assert match_manual(facts(), [usd], charge_date=CHARGE).outcome == "none"
        wrong_currency = expense(amount_minor=2000, currency="EUR")
        assert match_manual(with_original, [wrong_currency], charge_date=CHARGE).outcome == "none"

    def test_amount_must_be_equal_not_close(self):
        assert (
            match_manual(facts(), [expense(amount_minor=7501)], charge_date=CHARGE).outcome
            == "none"
        )

    @pytest.mark.parametrize(
        ("when", "outcome"),
        [
            ("2026-09-14", "one"),  # txn_date - 3 days, inclusive
            ("2026-09-13", "none"),
            ("2026-10-18", "one"),  # charge_date + 3 days, inclusive
            ("2026-10-19", "none"),
        ],
    )
    def test_date_window(self, when, outcome):
        assert match_manual(facts(), [expense(date=when)], charge_date=CHARGE).outcome == outcome

    def test_a_cancelled_expense_is_never_a_candidate(self):
        assert (
            match_manual(facts(), [expense(payment_status="בוטל")], charge_date=CHARGE).outcome
            == "none"
        )

    def test_an_unpaid_expense_can_still_be_a_candidate(self):
        assert (
            match_manual(facts(), [expense(payment_status="לא שולם")], charge_date=CHARGE).outcome
            == "one"
        )

    def test_statement_created_expenses_are_never_candidates(self):
        stmt = expense(source_key="stmt:STM_" + "a" * 32 + ":" + "b" * 64)
        assert match_manual(facts(), [stmt], charge_date=CHARGE).outcome == "none"

    def test_an_already_linked_expense_is_not_a_candidate(self):
        linked = expense(linked_line_key="c" * 64)
        assert match_manual(facts(), [linked], charge_date=CHARGE).outcome == "none"

    def test_vendor_must_match_the_key_or_an_alias(self):
        other = expense(vendor="Totally Different Shop")
        assert match_manual(facts(), [other], charge_date=CHARGE).outcome == "none"
        by_alias = expense(vendor="Anthropic")
        assert (
            match_manual(facts(), [by_alias], charge_date=CHARGE, aliases=("anthropic",)).outcome
            == "one"
        )

    def test_credits_are_never_merged(self):
        assert match_manual(facts(kind="credit"), [expense()], charge_date=CHARGE).outcome == "none"

    def test_an_entry_with_an_unrelated_vendor_stays_out(self):
        tip = expense(expense_id="tip", source_key="cash:tip", vendor="Cash tip")
        assert match_manual(facts(), [tip], charge_date=CHARGE).outcome == "none"

    def test_the_schema_has_no_cash_marker_so_a_cash_entry_matches_on_vendor_amount_and_date(self):
        # An expense has no payment-method field. Nothing here can tell a cash entry from a
        # card entry, so a matching one is a candidate and the owner approves the link.
        same = expense(expense_id="cash", source_key="cash:claude", vendor="Claude AI Subscription")
        assert match_manual(facts(), [same], charge_date=CHARGE).outcome == "one"

    @pytest.mark.parametrize("vendor", [None, "", "   "])
    def test_an_entry_without_a_vendor_never_matches(self, vendor):
        nameless = expense(vendor=vendor)
        assert match_manual(facts(), [nameless], charge_date=CHARGE).outcome == "none"
        assert not vendor_matches(vendor, "claude ai subscription", ("claude",))


class TestWholeBatchContention:
    def test_one_expense_cannot_be_the_sole_candidate_of_two_lines(self):
        # two identical same-day charges, one hand-entered expense: nothing auto-merges
        items = [
            MergeItem("k1", facts(), ()),
            MergeItem("k2", facts(), ()),
        ]
        result = match_batch(items, [expense()], charge_date=CHARGE)
        assert result["k1"].outcome == "choose" and result["k2"].outcome == "choose"
        assert result["k1"].candidate_ids == ("e1",)

    def test_uncontended_lines_still_get_their_single_link(self):
        items = [
            MergeItem("k1", facts(), ()),
            MergeItem(
                "k2",
                facts(merchant_key="other shop", merchant_display="OTHER SHOP", amount_minor=100),
                (),
            ),
        ]
        result = match_batch(items, [expense()], charge_date=CHARGE)
        assert result["k1"].outcome == "one" and result["k1"].expense_id == "e1"
        assert result["k2"].outcome == "none"

    def test_fixture_expenses_for_the_synthetic_cycle(self):
        manual = fixture_manual_expenses()
        claude = facts(original_amount_minor=2000, original_currency="USD")
        coffee = facts(
            txn_date=date(2026, 9, 23),
            merchant_display="COFFEE CART DEMO",
            merchant_key="coffee cart demo",
            amount_minor=1800,
        )
        result = match_batch(
            [MergeItem("claude", claude, ("claude", "anthropic")), MergeItem("coffee", coffee, ())],
            manual,
            charge_date=CHARGE,
        )
        assert result["claude"].outcome == "one"
        assert result["claude"].expense_id == "exp-manual-claude"
        assert result["coffee"].outcome == "choose"
        assert result["coffee"].candidate_ids == ("exp-manual-coffee-a", "exp-manual-coffee-b")


class TestManualExpenseIsValidated:
    """A bad hand-entry view fails with a reason code at construction, not deep inside a diff."""

    def build(self, **overrides):
        base = {
            "expense_id": "e1",
            "source_key": "manual:one",
            "date": date(2026, 9, 17),
            "vendor": "Claude",
            "amount_minor": 7500,
            "currency": "ILS",
            "payment_status": "שולם",
            "linked_line_key": None,
        }
        base.update(overrides)
        return ManualExpense(**base)

    def test_a_well_formed_entry_builds(self):
        assert self.build().vendor == "Claude"
        assert self.build(vendor=None).vendor is None
        assert self.build(linked_line_key="c" * 64).linked_line_key == "c" * 64

    @pytest.mark.parametrize(
        ("field", "value"),
        [
            ("expense_id", ""),
            ("expense_id", 5),
            ("source_key", None),
            ("source_key", ""),
            ("date", datetime(2026, 9, 17, 10, 30)),
            ("date", "2026-09-17"),
            ("date", None),
            ("vendor", 5),
            ("amount_minor", "7500"),
            ("amount_minor", 75.0),
            ("amount_minor", True),
            ("amount_minor", 0),
            ("amount_minor", -5),
            ("currency", "JPY"),
            ("currency", None),
            ("payment_status", "paid"),
            ("payment_status", None),
            ("linked_line_key", "not-a-line-key"),
            ("linked_line_key", 12),
        ],
    )
    def test_malformed_fields_are_refused_with_a_reason_code(self, field, value):
        with pytest.raises(StatementValidationError) as raised:
            self.build(**{field: value})
        assert (field, "invalid_expense") in {(i.path, i.code) for i in raised.value.issues}

    def test_the_error_never_echoes_the_value(self):
        with pytest.raises(StatementValidationError) as raised:
            self.build(vendor=5, amount_minor="987654")
        assert "987654" not in str(raised.value) and "987654" not in repr(raised.value.issues)
