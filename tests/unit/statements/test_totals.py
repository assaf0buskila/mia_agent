"""Business and personal totals, the per-cycle subscriptions list, and cycle import status."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from app.domain.statements import (
    OwnerOverride,
    StatementValidationError,
    cycle_status,
    subscription_view,
    summarize_cycle,
)

from tests.unit.statements.helpers import (
    commit_rows,
    fixture,
    fixture_rules,
    raw_batch,
    raw_line,
    run_import,
)


def october_rows(**kwargs):
    imp = run_import(fixture()["october"], rules=fixture_rules(), **kwargs)
    return imp, commit_rows(imp.diff)


def november_rows():
    imp = run_import(fixture()["november"], rules=fixture_rules())
    return imp, commit_rows(imp.diff)


class TestTotals:
    def test_business_total_nets_credits_and_personal_is_only_a_count(self):
        _, rows = october_rows()
        totals = summarize_cycle(rows)
        assert totals.business_charge_minor == 172200
        assert totals.business_credit_minor == 5000
        assert totals.business_total_minor == 167200
        assert totals.business_line_count == 9
        assert totals.personal_count == 3
        assert totals.unassigned_count == 0

    def test_personal_amounts_are_unknown_while_the_minimisation_fork_is_on(self):
        _, rows = october_rows()
        assert summarize_cycle(rows).personal_total_minor is None

    def test_personal_total_exists_when_the_fork_is_off(self):
        _, rows = october_rows(minimize_personal=False)
        assert summarize_cycle(rows).personal_total_minor == 31250 + 8900 + 4300

    def test_totals_by_category_net_credits_and_sort_by_size(self):
        _, rows = october_rows()
        by_category = summarize_cycle(rows).by_category
        assert dict(by_category) == {
            "פרסום": 120000,
            "ציוד": 24500 - 5000,
            "כלי AI": 7500 + 7400 + 3700,
            "עריכת וידאו": 5500,
            "אירוח": 3600,
        }
        assert [name for name, _ in by_category] == [
            "פרסום",
            "ציוד",
            "כלי AI",
            "עריכת וידאו",
            "אירוח",
        ]

    def test_replaced_and_voided_lines_do_not_count(self):
        first, prev = october_rows()
        raw = fixture()["october"]
        raw["lines"] = [x for x in raw["lines"] if x["merchant_display"] != "META ADS DEMO"]
        again = run_import(raw, previous=prev, rules=fixture_rules())
        totals = summarize_cycle(again.diff.next_lines)
        assert totals.business_total_minor == 167200 - 120000
        assert totals.business_line_count == 8

    def test_a_cycle_with_no_committed_statement_is_null_never_zero(self):
        assert summarize_cycle(None) is None

    def test_a_committed_statement_with_no_active_lines_is_a_real_zero(self):
        totals = summarize_cycle([])
        assert totals is not None and totals.business_total_minor == 0

    def test_unassigned_lines_are_counted_separately_and_never_summed(self):
        imp = run_import(
            raw_batch(
                [
                    raw_line(
                        merchant_display="MYSTERY SHOP", suggested_tag=None, amount_minor=5000
                    ),
                    raw_line(merchant_display="KNOWN SHOP", amount_minor=700),
                ]
            )
        )
        totals = summarize_cycle(imp.diff.next_lines)
        assert totals.unassigned_count == 1 and totals.business_total_minor == 700

    def test_owner_flips_move_a_line_between_the_totals(self):
        first, prev = october_rows()
        key = first.key("META ADS DEMO")
        flipped = run_import(
            fixture()["october"],
            previous=prev,
            rules=fixture_rules(),
            overrides={key: OwnerOverride(tag="personal")},
        )
        totals = summarize_cycle(flipped.diff.next_lines)
        assert totals.business_total_minor == 167200 - 120000
        assert totals.personal_count == 4

    def test_totals_are_integers_in_minor_units(self):
        _, rows = october_rows()
        totals = summarize_cycle(rows)
        assert all(
            isinstance(v, int)
            for v in (
                totals.business_total_minor,
                totals.business_charge_minor,
                totals.business_credit_minor,
            )
        )


class TestSubscriptionsList:
    def test_the_first_cycle_lists_the_business_subscriptions_without_change_badges(self):
        _, rows = october_rows()
        view = subscription_view(rows, None, fixture_rules())
        assert [row.label for row in view] == ["CapCut", "ChatGPT", "Claude", "Grok"]
        assert all(row.badges == () for row in view)
        claude = next(row for row in view if row.label == "Claude")
        assert (claude.amount_minor, claude.original_amount_minor, claude.original_currency) == (
            7500,
            2000,
            "USD",
        )
        assert claude.line_count == 1 and claude.previous_amount_minor is None

    def test_non_subscription_and_personal_lines_are_never_listed(self):
        _, rows = october_rows()
        labels = {row.label for row in subscription_view(rows, None, fixture_rules())}
        assert not labels & {"META ADS DEMO", "COFFEE CART DEMO", "SUPERMARKET DEMO"}

    def test_new_missing_and_changed_badges_against_the_previous_cycle(self):
        _, october = october_rows()
        _, november = november_rows()
        view = {row.label: row for row in subscription_view(november, october, fixture_rules())}
        assert list(view) == sorted(view, key=str.casefold)
        assert view["Claude"].badges == ()
        assert view["CapCut"].badges == ()
        assert view["ChatGPT"].badges == ("changed",)
        assert (view["ChatGPT"].amount_minor, view["ChatGPT"].previous_amount_minor) == (7800, 7400)
        assert view["NEW TOOL DEMO"].badges == ("new",)
        assert view["Grok"].badges == ("missing",)
        assert view["Grok"].amount_minor is None and view["Grok"].line_count == 0

    def test_an_unknown_previous_cycle_gives_no_new_badge(self):
        _, november = november_rows()
        view = {row.label: row for row in subscription_view(november, None, fixture_rules())}
        assert view["NEW TOOL DEMO"].badges == ()

    def test_an_empty_previous_cycle_marks_everything_new(self):
        _, november = november_rows()
        view = subscription_view(november, [], fixture_rules())
        assert all("new" in row.badges for row in view if row.label != "Grok")

    def test_rules_that_are_inactive_or_not_subscriptions_never_show_as_missing(self):
        _, november = november_rows()
        labels = {row.label for row in subscription_view(november, [], fixture_rules())}
        assert "META ADS DEMO" not in labels and "SUPERMARKET DEMO" not in labels

    def test_two_charges_from_one_merchant_in_a_cycle_are_summed(self):
        imp = run_import(
            raw_batch(
                [
                    raw_line(
                        merchant_display="TOOL SEAT",
                        is_subscription=True,
                        amount_minor=5000,
                        original_amount_minor=1400,
                        original_currency="USD",
                    ),
                    raw_line(
                        merchant_display="TOOL SEAT",
                        is_subscription=True,
                        amount_minor=5000,
                        original_amount_minor=1400,
                        original_currency="USD",
                    ),
                ]
            )
        )
        [row] = subscription_view(imp.diff.next_lines, None)
        assert (row.amount_minor, row.line_count, row.original_amount_minor) == (10000, 2, 2800)

    def test_mixed_original_currencies_have_no_original_total(self):
        imp = run_import(
            raw_batch(
                [
                    raw_line(
                        merchant_display="TOOL SEAT",
                        is_subscription=True,
                        amount_minor=5000,
                        original_amount_minor=1400,
                        original_currency="USD",
                    ),
                    raw_line(
                        merchant_display="TOOL SEAT",
                        is_subscription=True,
                        amount_minor=5100,
                        original_amount_minor=1300,
                        original_currency="EUR",
                    ),
                ]
            )
        )
        [row] = subscription_view(imp.diff.next_lines, None)
        assert (row.original_amount_minor, row.original_currency) == (None, None)

    def test_an_owner_can_unmark_a_subscription_for_one_line(self):
        first, rows = october_rows()
        key = first.key("CAPCUT")
        again = run_import(
            fixture()["october"],
            previous=rows,
            rules=fixture_rules(),
            overrides={key: OwnerOverride(is_subscription=False)},
        )
        labels = {
            row.label for row in subscription_view(again.diff.next_lines, None, fixture_rules())
        }
        assert "CapCut" not in labels and {"Claude", "ChatGPT"} <= labels


class TestCycleStatus:
    def test_imported_cycles(self):
        assert (
            cycle_status("2026-10", committed_cycles={"2026-10"}, today=date(2026, 10, 30))
            == "imported"
        )

    def test_a_cycle_not_yet_due_is_awaiting(self):
        assert (
            cycle_status("2026-10", committed_cycles=set(), today=date(2026, 10, 12)) == "awaiting"
        )

    def test_the_twentieth_itself_is_still_awaiting_and_after_it_the_cycle_is_not_imported(self):
        assert (
            cycle_status("2026-10", committed_cycles=set(), today=date(2026, 10, 20)) == "awaiting"
        )
        assert (
            cycle_status("2026-10", committed_cycles=set(), today=date(2026, 10, 21))
            == "not_imported"
        )

    def test_a_future_cycle_is_awaiting(self):
        assert (
            cycle_status("2026-12", committed_cycles=set(), today=date(2026, 10, 30)) == "awaiting"
        )

    def test_the_import_deadline_is_a_parameter(self):
        assert (
            cycle_status(
                "2026-10", committed_cycles=set(), today=date(2026, 10, 24), import_by_day=25
            )
            == "awaiting"
        )

    @pytest.mark.parametrize("bad", ["2026-1", "202610", "2026-13", "", "x"])
    def test_invalid_cycle_labels_are_rejected(self, bad):
        with pytest.raises(StatementValidationError):
            cycle_status(bad, committed_cycles=set(), today=date(2026, 10, 1))

    @pytest.mark.parametrize("bad", ["2026-10", b"2026-10", None, 202610])
    def test_committed_cycles_must_be_a_collection_not_a_string(self, bad):
        # a plain string would answer "is this cycle a substring" instead of "is it in the set"
        with pytest.raises(StatementValidationError) as raised:
            cycle_status("2026-10", committed_cycles=bad, today=date(2026, 10, 1))
        assert raised.value.issues[0].code == "invalid_cycles"

    @pytest.mark.parametrize("bad_day", [0, 29, 31, "20"])
    def test_invalid_deadline_days_are_rejected(self, bad_day):
        with pytest.raises(StatementValidationError):
            cycle_status(
                "2026-10", committed_cycles=set(), today=date(2026, 10, 1), import_by_day=bad_day
            )

    @pytest.mark.parametrize("bad_today", ["2026-10-01", None, datetime(2026, 10, 1, 12, 0)])
    def test_today_must_be_a_plain_date(self, bad_today):
        with pytest.raises(StatementValidationError):
            cycle_status("2026-10", committed_cycles=set(), today=bad_today)
