"""Billing-cycle identity, deterministic keyed line keys, merchant normalization, card guard."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date

import pytest
from app.domain.statements import (
    LineFacts,
    StatementValidationError,
    contains_card_like_digits,
    cycle_of,
    line_key,
    luhn_valid,
    normalize_merchant,
    statement_id,
)

from tests.unit.statements.helpers import OTHER_PEPPER, PEPPER, WORKSPACE


def facts(**overrides) -> LineFacts:
    base = {
        "txn_date": date(2026, 9, 17),
        "merchant_display": "Claude AI",
        "merchant_key": "claude ai",
        "amount_minor": 7500,
        "kind": "charge",
        "currency": "ILS",
    }
    base.update(overrides)
    return LineFacts(**base)


class TestNormalizeMerchant:
    def test_case_punctuation_and_whitespace_collapse(self):
        assert normalize_merchant("  OpenAI  *ChatGPT   Subscr. ") == "openai chatgpt subscr"

    def test_hebrew_is_kept_and_points_and_bidi_marks_are_dropped(self):
        assert normalize_merchant("מַכּוֹלֶת‏  לדוגמה") == "מכולת לדוגמה"

    def test_compatibility_forms_fold(self):
        assert normalize_merchant("ＣＡＰＣＵＴ") == "capcut"

    def test_idempotent(self):
        for text in ("X.AI GROK", "  מכולת   לדוגמה ", "McDonald's #12", "CLAUDE.AI"):
            once = normalize_merchant(text)
            assert normalize_merchant(once) == once

    def test_punctuation_only_normalizes_to_empty(self):
        assert normalize_merchant("***  --") == ""


class TestCardNumberGuard:
    def test_eight_digit_run_is_card_like(self):
        assert contains_card_like_digits("ref 12345678")
        assert not contains_card_like_digits("ref 1234567")

    def test_luhn_valid_sixteen_digits_contiguous(self):
        assert contains_card_like_digits("4111111111111111")

    @pytest.mark.parametrize("text", ["4111 1111 1111 1111", "4111-1111-1111-1111"])
    def test_grouped_luhn_valid_number_is_card_like(self, text):
        assert contains_card_like_digits(text)

    @pytest.mark.parametrize(
        "text",
        [
            "4111.1111.1111.1111",
            "4111/1111/1111/1111",
            "4111_1111_1111_1111",
            "4111,1111,1111,1111",
            "4111 - 1111 - 1111 - 1111",
            "4111 • 1111 • 1111 • 1111",
            "4111​1111​1111​1111",  # zero-width separators
            "3782.822463.10005",  # 4-6-5 grouping, dots
            "card 4111.1111.1111.1111 end",
        ],
    )
    def test_any_punctuation_between_digit_groups_still_reads_as_one_number(self, text):
        assert contains_card_like_digits(text)

    def test_digits_split_by_letters_are_not_one_number(self):
        assert not contains_card_like_digits("4111a1111a1111a1111")
        assert not contains_card_like_digits("4111 1111 shop 1111 1111")

    def test_grouped_luhn_invalid_number_is_not_card_like(self):
        assert not luhn_valid("1234567890123456")
        assert not contains_card_like_digits("1234 5678 9012 3456")

    def test_grouped_valid_number_inside_longer_digit_sequence(self):
        assert contains_card_like_digits("id 99 4111 1111 1111 1111 99")

    def test_non_ascii_digits_are_folded_before_checking(self):
        assert contains_card_like_digits("４１１１１１１１１１１１１１１１")  # fullwidth
        assert contains_card_like_digits("٤١١١١١١١١")  # arabic-indic, 8 digits

    @pytest.mark.parametrize(
        "text", ["Invoice 2026-10-15 #42", "7-ELEVEN", "מכולת 12", "call 050 123 456"]
    )
    def test_ordinary_merchant_text_is_not_card_like(self, text):
        assert not contains_card_like_digits(text)

    def test_luhn_check(self):
        assert luhn_valid("4111111111111111")
        assert not luhn_valid("4111111111111112")
        assert not luhn_valid("411111111111111x")
        assert not luhn_valid("")


class TestCycle:
    def test_cycle_is_the_charge_month_not_the_transaction_month(self):
        assert cycle_of(date(2026, 10, 15)) == "2026-10"
        assert cycle_of("2026-12-31") == "2026-12"

    def test_a_shifted_charge_date_stays_in_its_month(self):
        # weekends and holidays move the charge date; the 15th is never assumed
        assert cycle_of(date(2026, 10, 16)) == "2026-10"
        assert cycle_of(date(2026, 10, 12)) == "2026-10"

    @pytest.mark.parametrize("bad", ["2026-1-5", "20261015", "2026-02-30", "", "x", 20261015, None])
    def test_invalid_dates_are_rejected(self, bad):
        with pytest.raises(StatementValidationError):
            cycle_of(bad)


class TestStatementId:
    def test_format_and_determinism(self):
        sid = statement_id(PEPPER, WORKSPACE, "Synthetic Business Card", date(2026, 10, 15))
        assert sid.startswith("STM_") and len(sid) == 36
        assert sid == statement_id(PEPPER, WORKSPACE, "Synthetic Business Card", "2026-10-15")

    def test_account_label_spacing_and_case_do_not_change_the_cycle_identity(self):
        a = statement_id(PEPPER, WORKSPACE, "Synthetic Business Card", date(2026, 10, 15))
        b = statement_id(PEPPER, WORKSPACE, "  synthetic   BUSINESS card ", date(2026, 10, 15))
        assert a == b

    def test_each_identity_part_matters(self):
        base = statement_id(PEPPER, WORKSPACE, "Card A", date(2026, 10, 15))
        assert base != statement_id(PEPPER, "ws_other", "Card A", date(2026, 10, 15))
        assert base != statement_id(PEPPER, WORKSPACE, "Card B", date(2026, 10, 15))
        assert base != statement_id(PEPPER, WORKSPACE, "Card A", date(2026, 10, 16))
        assert base != statement_id(OTHER_PEPPER, WORKSPACE, "Card A", date(2026, 10, 15))

    def test_account_label_with_a_card_number_is_rejected_without_echoing_it(self):
        with pytest.raises(StatementValidationError) as raised:
            statement_id(PEPPER, WORKSPACE, "Visa 4111 1111 1111 1111", date(2026, 10, 15))
        assert "4111" not in str(raised.value)

    @pytest.mark.parametrize("pepper", [b"", b"short", "not-bytes", None])
    def test_a_missing_or_short_pepper_fails_closed(self, pepper):
        with pytest.raises(StatementValidationError) as raised:
            statement_id(pepper, WORKSPACE, "Card A", date(2026, 10, 15))
        assert raised.value.issues[0].code == "pepper_invalid"


class TestLineKey:
    SID = "STM_" + "a" * 32

    def key(self, **overrides) -> str:
        occurrence = overrides.pop("occurrence_index", 0)
        pepper = overrides.pop("pepper", PEPPER)
        return line_key(pepper, self.SID, facts(**overrides), occurrence)

    def test_is_a_stable_64_hex_digest(self):
        assert self.key() == self.key()
        assert len(self.key()) == 64 and set(self.key()) <= set("0123456789abcdef")

    def test_is_keyed_not_a_plain_hash(self):
        # a different pepper must not reproduce the key, so a stored key-only personal row
        # cannot be brute-forced back to merchant and amount without the pepper
        assert self.key() != self.key(pepper=OTHER_PEPPER)
        plain = hashlib.sha256(b"claude ai|7500|2026-09-17").hexdigest()
        assert self.key() != plain

    def test_short_pepper_is_rejected(self):
        with pytest.raises(StatementValidationError):
            self.key(pepper=b"short")

    def test_every_identity_field_changes_the_key(self):
        base = self.key()
        variants = [
            self.key(txn_date=date(2026, 9, 18)),
            self.key(merchant_key="claude"),
            self.key(amount_minor=7501),
            self.key(kind="credit"),
            self.key(original_amount_minor=2000, original_currency="USD"),
            self.key(installment_n=1, installment_of=3),
            self.key(installment_n=2, installment_of=3),
            self.key(occurrence_index=1),
        ]
        assert len({base, *variants}) == len(variants) + 1

    def test_display_text_is_not_part_of_the_key(self):
        assert self.key(merchant_display="CLAUDE.AI SUBSCRIPTION") == self.key()

    def test_facts_only_hold_cleaned_bounded_text(self):
        # the expense vendor is built from the display, and the OS service rejects unstripped text
        for display in ("CLAUDE.AI  SUBSCRIPTION", " padded", "line\nbreak", "x" * 81, ""):
            with pytest.raises(StatementValidationError):
                facts(merchant_display=display)
        with pytest.raises(StatementValidationError):
            facts(merchant_key="Not Normalized")

    def test_statement_id_is_part_of_the_key(self):
        other = line_key(PEPPER, "STM_" + "b" * 32, facts(), 0)
        assert other != self.key()

    def test_facts_are_immutable(self):
        with pytest.raises(AttributeError):
            replace(facts(), amount_minor=1).amount_minor = 2  # type: ignore[misc]
