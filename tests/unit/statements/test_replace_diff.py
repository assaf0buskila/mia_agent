"""Replace semantics: a re-import touches only that statement's earlier bank lines."""

from __future__ import annotations

import random
from dataclasses import replace

import pytest
from app.domain.statements import (
    OwnerOverride,
    ReviewChoices,
    StatementValidationError,
    apply_rules,
    diff_statement,
    owns_source_key,
    statement_source_key,
)

from tests.unit.statements.helpers import (
    commit_rows,
    entry_of,
    fixture,
    fixture_manual_expenses,
    fixture_rules,
    ops_of,
    parse,
    raw_batch,
    raw_line,
    run_import,
)

CLAUDE = "CLAUDE.AI SUBSCRIPTION"
CAPCUT = "CAPCUT"
COFFEE = "COFFEE CART DEMO"
SUPERMARKET = "SUPERMARKET DEMO"
OFFICE = "OFFICE SUPPLY DEMO"


def october_import(**kwargs):
    return run_import(fixture()["october"], rules=fixture_rules(), **kwargs)


def committed_october(**kwargs):
    first = october_import(**kwargs)
    return first, commit_rows(first.diff)


def row_of(rows, line_key):
    [row] = [r for r in rows if r.line_key == line_key]
    return row


def without(lines, merchant_display, nth=0):
    lines = list(lines)
    seen = -1
    for index, line in enumerate(lines):
        if " ".join(line["merchant_display"].split()) == merchant_display:
            seen += 1
            if seen == nth:
                del lines[index]
                return lines
    raise AssertionError("line not found")


class TestFirstImport:
    def test_every_line_is_added_and_business_charges_create_expenses(self):
        diff = october_import().diff
        assert len(diff.entries) == 12 and {e.action for e in diff.entries} == {"add"}
        assert len(diff.ops) == 8 and {op.op for op in diff.ops} == {"create"}
        assert diff.blocking == ()
        assert not diff.is_noop

    def test_credits_are_business_lines_without_an_expense(self):
        imp = october_import()
        credit = row_of(imp.diff.next_lines, imp.key("OFFICE SUPPLY DEMO RETURN"))
        assert (credit.tag, credit.facts.kind, credit.expense_id) == ("business", "credit", None)
        assert imp.key("OFFICE SUPPLY DEMO RETURN") not in {op.line_key for op in imp.diff.ops}

    def test_created_drafts_follow_the_mapping_and_the_rule_platform(self):
        imp = october_import()
        sid = imp.batch.statement_id
        by_key = {op.line_key: op for op in imp.diff.ops}
        meta = by_key[imp.key("META ADS DEMO")].draft
        assert meta.platform == "Meta" and meta.path == "אוטומציה" and meta.category == "פרסום"
        assert meta.date == imp.batch.charge_date and meta.amount_ils == 120000
        claude_op = by_key[imp.key(CLAUDE)]
        assert claude_op.source_key == statement_source_key(sid, imp.key(CLAUDE))
        assert all(owns_source_key(sid, op.source_key) for op in imp.diff.ops)

    def test_the_expense_date_basis_is_a_parameter(self):
        imp = october_import(date_basis="txn_date")
        claude_op = ops_of(imp.diff, "create")
        by_key = {op.line_key: op for op in claude_op}
        assert str(by_key[imp.key(CLAUDE)].draft.date) == "2026-09-17"

    def test_unassigned_lines_block_approval_and_create_nothing(self):
        lines = [raw_line(merchant_display="MYSTERY SHOP", suggested_tag=None)]
        imp = run_import(raw_batch(lines))
        assert imp.diff.blocking == ("unassigned_lines",)
        assert imp.diff.ops == ()
        [entry] = imp.diff.entries
        assert entry.action == "add" and "unassigned" in entry.flags
        assert imp.diff.next_lines[0].tag is None


class TestIdenticalReimport:
    def test_an_identical_reimport_is_zero_mutations(self):
        _, prev = committed_october()
        again = october_import(previous=prev).diff
        assert again.ops == () and again.line_writes == ()
        assert {e.action for e in again.entries} == {"keep"}
        assert again.is_noop and again.blocking == ()

    def test_reordering_and_a_new_batch_id_change_nothing(self):
        _, prev = committed_october()
        lines = list(fixture()["october"]["lines"])
        random.Random(11).shuffle(lines)
        again = run_import(
            raw_batch(lines, client_batch_id="9c1d3e5f-1111-4222-8333-444455556666"),
            previous=prev,
            rules=fixture_rules(),
        ).diff
        assert again.is_noop

    def test_a_changed_confidence_alone_is_not_a_mutation(self):
        _, prev = committed_october()
        raw = fixture()["october"]
        raw["lines"] = [{**line, "confidence": 0.5} for line in raw["lines"]]
        assert run_import(raw, previous=prev, rules=fixture_rules()).diff.is_noop

    @pytest.mark.parametrize("seed", range(25))
    def test_property_identical_reimport_never_mutates(self, seed):
        raw = random_statement(seed)
        first = run_import(raw)
        prev = commit_rows(first.diff)
        lines = list(raw["lines"])
        random.Random(seed).shuffle(lines)
        again = run_import(raw_batch(lines), previous=prev)
        assert again.diff.is_noop, seed
        assert len({r.line_key for r in prev}) == len(raw["lines"])


def random_statement(seed):
    rnd = random.Random(seed)
    pool = [
        ("ALPHA TOOLS", "business"),
        ("BETA CLOUD", "business"),
        ("GAMMA CAFE", "business"),
        ("DELTA MARKET", "personal"),
        ("EPSILON PHARM", "personal"),
        ("ZETA ADS", "business"),
    ]
    lines = []
    for _ in range(rnd.randint(5, 40)):
        name, tag = rnd.choice(pool)
        lines.append(
            raw_line(
                merchant_display=name,
                txn_date=f"2026-09-{rnd.randint(16, 30)}",
                amount_minor=rnd.choice([1000, 2500, 9900]),
                suggested_tag=tag,
                kind=rnd.choice(["charge", "charge", "charge", "credit"]),
            )
        )
    return raw_batch(lines)


class TestRemovedLines:
    def test_one_removed_business_line_gives_exactly_one_void(self):
        first, prev = committed_october()
        raw = fixture()["october"]
        raw["lines"] = without(raw["lines"], CLAUDE)
        imp = run_import(raw, previous=prev, rules=fixture_rules())
        key = first.key(CLAUDE)
        voids = [e for e in imp.diff.entries if e.action == "void"]
        assert [e.line_key for e in voids] == [key] and voids[0].reason == "missing_from_import"
        [op] = imp.diff.ops
        assert (op.op, op.line_key, op.reason) == ("void", key, "statement_replaced")
        assert op.expense_id == row_of(prev, key).expense_id
        assert op.source_key == statement_source_key(first.batch.statement_id, key)
        assert row_of(imp.diff.next_lines, key).state == "replaced"
        assert len(imp.diff.line_writes) == 1
        assert {e.action for e in imp.diff.entries if e.line_key != key} == {"keep"}

    def test_a_removed_personal_line_is_replaced_without_touching_any_expense(self):
        first, prev = committed_october()
        raw = fixture()["october"]
        raw["lines"] = without(raw["lines"], SUPERMARKET)
        imp = run_import(raw, previous=prev, rules=fixture_rules())
        key = first.key(SUPERMARKET)
        assert imp.diff.ops == ()
        assert entry_of(imp.diff, key).action == "void"
        assert row_of(imp.diff.next_lines, key).state == "replaced"
        assert row_of(imp.diff.next_lines, key).facts is None

    def test_a_removed_credit_is_replaced_without_an_expense_op(self):
        first, prev = committed_october()
        raw = fixture()["october"]
        raw["lines"] = without(raw["lines"], "OFFICE SUPPLY DEMO RETURN")
        imp = run_import(raw, previous=prev, rules=fixture_rules())
        assert imp.diff.ops == ()
        assert entry_of(imp.diff, first.key("OFFICE SUPPLY DEMO RETURN")).action == "void"

    def test_removing_one_of_two_identical_charges_voids_only_the_second_occurrence(self):
        first, prev = committed_october()
        raw = fixture()["october"]
        raw["lines"] = without(raw["lines"], COFFEE)
        imp = run_import(raw, previous=prev, rules=fixture_rules())
        voids = [e for e in imp.diff.entries if e.action == "void"]
        assert len(voids) == 1 and len(imp.diff.ops) == 1
        assert voids[0].line_key == first.key(COFFEE, 1)

    @pytest.mark.parametrize("seed", range(25))
    def test_property_one_removed_line_is_one_void(self, seed):
        raw = random_statement(seed)
        first = run_import(raw)
        prev = commit_rows(first.diff)
        victim = random.Random(seed + 1000).randrange(len(raw["lines"]))
        smaller = raw_batch(raw["lines"][:victim] + raw["lines"][victim + 1 :])
        if not smaller["lines"]:
            return
        imp = run_import(smaller, previous=prev)
        voids = [e for e in imp.diff.entries if e.action == "void"]
        assert len(voids) == 1, seed
        row = row_of(prev, voids[0].line_key)
        expected_ops = 1 if row.tag == "business" and row.facts.kind == "charge" else 0
        assert len(imp.diff.ops) == expected_ops
        assert all(e.action == "keep" for e in imp.diff.entries if e is not voids[0])


class TestTagFlips:
    def test_business_to_personal_voids_the_expense_and_keeps_a_key_only_personal_row(self):
        first, prev = committed_october()
        key = first.key(CLAUDE)
        imp = run_import(
            fixture()["october"],
            previous=prev,
            rules=fixture_rules(),
            overrides={key: OwnerOverride(tag="personal")},
        )
        entry = entry_of(imp.diff, key)
        assert (entry.action, entry.reason, entry.from_tag, entry.to_tag) == (
            "void",
            "became_personal",
            "business",
            "personal",
        )
        [op] = imp.diff.ops
        assert (op.op, op.expense_id, op.reason) == (
            "void",
            row_of(prev, key).expense_id,
            "statement_replaced",
        )
        row = row_of(imp.diff.next_lines, key)
        assert (row.tag, row.state) == ("personal", "active")
        assert row.facts is None and row.classification is None
        assert row.owner_override == OwnerOverride(tag="personal")

    def test_personal_to_business_creates_the_expense(self):
        first, prev = committed_october()
        key = first.key(SUPERMARKET)
        imp = run_import(
            fixture()["october"],
            previous=prev,
            rules=fixture_rules(),
            overrides={key: OwnerOverride(tag="business", category="ציוד")},
        )
        entry = entry_of(imp.diff, key)
        assert (entry.action, entry.reason) == ("add", "became_business")
        [op] = imp.diff.ops
        assert op.op == "create" and op.line_key == key and op.draft.category == "ציוד"
        row = row_of(imp.diff.next_lines, key)
        assert row.tag == "business" and row.facts.amount_minor == 31250

    def test_a_flip_to_personal_and_back_revives_the_cancelled_expense(self):
        first, prev = committed_october()
        key = first.key(CLAUDE)
        original_expense = row_of(prev, key).expense_id
        step2 = run_import(
            fixture()["october"],
            previous=prev,
            rules=fixture_rules(),
            overrides={key: OwnerOverride(tag="personal")},
        )
        prev2 = commit_rows(step2.diff)
        assert row_of(prev2, key).expense_id == original_expense  # opaque id survives, no detail
        step3 = run_import(
            fixture()["october"],
            previous=prev2,
            rules=fixture_rules(),
            overrides={key: OwnerOverride(tag="business")},
        )
        [op] = step3.diff.ops
        assert (op.op, op.expense_id, op.line_key) == ("revive", original_expense, key)
        assert op.draft.amount_ils == 7500

    def test_a_removed_line_that_comes_back_revives_instead_of_inserting_a_duplicate(self):
        first, prev = committed_october()
        key = first.key(CLAUDE)
        raw = fixture()["october"]
        smaller = {**raw, "lines": without(raw["lines"], CLAUDE)}
        prev2 = commit_rows(run_import(smaller, previous=prev, rules=fixture_rules()).diff)
        back = run_import(raw, previous=prev2, rules=fixture_rules())
        [op] = back.diff.ops
        assert (op.op, op.expense_id) == ("revive", row_of(prev, key).expense_id)
        entry = entry_of(back.diff, key)
        assert (entry.action, entry.reason) == ("add", "reappeared")
        assert row_of(back.diff.next_lines, key).state == "active"


class TestFieldUpdates:
    def test_a_changed_category_updates_the_expense(self):
        first, prev = committed_october()
        raw = fixture()["october"]
        raw["lines"] = [
            {**x, "suggested_category": "כיבוד"} if x["merchant_display"] == OFFICE else x
            for x in raw["lines"]
        ]
        imp = run_import(raw, previous=prev, rules=fixture_rules())
        key = first.key(OFFICE)
        assert entry_of(imp.diff, key).action == "update"
        [op] = imp.diff.ops
        assert (op.op, op.expense_id) == ("update", row_of(prev, key).expense_id)
        assert op.draft.category == "כיבוד"
        assert row_of(imp.diff.next_lines, key).revision == row_of(prev, key).revision + 1

    def test_a_changed_display_with_the_same_merchant_updates_the_vendor(self):
        first, prev = committed_october()
        raw = fixture()["october"]
        raw["lines"] = [
            {**x, "merchant_display": "Capcut"} if x["merchant_display"] == CAPCUT else x
            for x in raw["lines"]
        ]
        imp = run_import(raw, previous=prev, rules=fixture_rules())
        [op] = imp.diff.ops
        assert op.op == "update" and op.draft.vendor == "Capcut"

    def test_a_new_line_is_added_next_to_unchanged_ones(self):
        _, prev = committed_october()
        raw = fixture()["october"]
        raw["lines"] = [
            *raw["lines"],
            raw_line(merchant_display="BRAND NEW DEMO", amount_minor=999),
        ]
        imp = run_import(raw, previous=prev, rules=fixture_rules())
        assert [e.action for e in imp.diff.entries].count("add") == 1
        assert [op.op for op in imp.diff.ops] == ["create"]

    def test_a_third_identical_charge_adds_only_the_new_occurrence(self):
        first, prev = committed_october()
        raw = fixture()["october"]
        coffee = next(x for x in raw["lines"] if x["merchant_display"] == COFFEE)
        raw["lines"] = [*raw["lines"], dict(coffee)]
        imp = run_import(raw, previous=prev, rules=fixture_rules())
        assert [e.action for e in imp.diff.entries].count("add") == 1
        assert len(imp.diff.ops) == 1 and imp.diff.ops[0].op == "create"


class TestOwnerEditedExpenses:
    def edited(self, prev, key):
        row = row_of(prev, key)
        return [
            replace(r, expense_revision=row.imported_expense_revision + 1)
            if r.line_key == key
            else r
            for r in prev
        ]

    def test_an_owner_edited_expense_is_kept_by_default_when_an_update_would_apply(self):
        first, prev = committed_october()
        key = first.key(CLAUDE)
        prev = self.edited(prev, key)
        imp = run_import(
            fixture()["october"],
            previous=prev,
            rules=fixture_rules(),
            overrides={key: OwnerOverride(category="תוכנה")},
        )
        entry = entry_of(imp.diff, key)
        assert (entry.action, entry.held_back) == ("owner_edited_kept", "update")
        assert imp.diff.ops == ()
        assert row_of(imp.diff.next_lines, key) == row_of(prev, key)
        assert imp.diff.line_writes == ()

    def test_an_owner_edited_expense_is_kept_when_the_line_goes_missing(self):
        first, prev = committed_october()
        key = first.key(CLAUDE)
        prev = self.edited(prev, key)
        raw = fixture()["october"]
        raw["lines"] = without(raw["lines"], CLAUDE)
        imp = run_import(raw, previous=prev, rules=fixture_rules())
        entry = entry_of(imp.diff, key)
        assert (entry.action, entry.held_back) == ("owner_edited_kept", "void")
        assert imp.diff.ops == ()
        assert row_of(imp.diff.next_lines, key).state == "active"

    def test_an_owner_edited_expense_is_kept_when_the_tag_would_become_personal(self):
        first, prev = committed_october()
        key = first.key(CLAUDE)
        prev = self.edited(prev, key)
        imp = run_import(
            fixture()["october"],
            previous=prev,
            rules=fixture_rules(),
            overrides={key: OwnerOverride(tag="personal")},
        )
        assert entry_of(imp.diff, key).action == "owner_edited_kept"
        assert imp.diff.ops == ()
        assert row_of(imp.diff.next_lines, key).tag == "business"

    def test_an_explicit_overwrite_choice_goes_ahead(self):
        first, prev = committed_october()
        key = first.key(CLAUDE)
        prev = self.edited(prev, key)
        imp = run_import(
            fixture()["october"],
            previous=prev,
            rules=fixture_rules(),
            overrides={key: OwnerOverride(category="תוכנה")},
            choices=ReviewChoices(owner_edited={key: "overwrite"}),
        )
        assert entry_of(imp.diff, key).action == "update"
        assert [op.op for op in imp.diff.ops] == ["update"]

    def test_an_explicit_keep_choice_is_the_same_as_the_default(self):
        first, prev = committed_october()
        key = first.key(CLAUDE)
        prev = self.edited(prev, key)
        imp = run_import(
            fixture()["october"],
            previous=prev,
            rules=fixture_rules(),
            overrides={key: OwnerOverride(category="תוכנה")},
            choices=ReviewChoices(owner_edited={key: "keep"}),
        )
        assert imp.diff.ops == ()

    def test_an_unedited_expense_is_not_reported_as_owner_edited(self):
        first, prev = committed_october()
        key = first.key(CLAUDE)
        imp = run_import(
            fixture()["october"],
            previous=prev,
            rules=fixture_rules(),
            overrides={key: OwnerOverride(category="תוכנה")},
        )
        assert entry_of(imp.diff, key).action == "update"

    def test_unknown_choice_values_and_lines_are_rejected(self):
        first, prev = committed_october()
        key = first.key(CLAUDE)
        for choices in (
            ReviewChoices(owner_edited={key: "maybe"}),
            ReviewChoices(owner_edited={"f" * 64: "keep"}),
            ReviewChoices(merge={"f" * 64: None}),
        ):
            with pytest.raises(StatementValidationError):
                october_import(previous=prev, choices=choices)

    @pytest.mark.parametrize("seed", range(20))
    def test_property_an_edited_expense_is_never_changed_unless_overwrite_is_chosen(self, seed):
        rnd = random.Random(seed)
        first, prev = committed_october()
        business_keys = sorted(
            r.line_key for r in prev if r.tag == "business" and r.expense_id is not None
        )
        edited = set(rnd.sample(business_keys, rnd.randint(1, len(business_keys))))
        for key in edited:
            prev = self.edited(prev, key)
        lines = list(fixture()["october"]["lines"])
        for _ in range(rnd.randint(0, 3)):
            lines.pop(rnd.randrange(len(lines)))
        raw = {**fixture()["october"], "lines": lines}
        present = sorted(x.line_key for x in parse(raw).lines)
        overrides = {
            key: OwnerOverride(
                tag=rnd.choice(["business", "personal"]), category=f"category {seed}"
            )
            for key in rnd.sample(present, min(6, len(present)))
        }
        imp = run_import(raw, previous=prev, rules=fixture_rules(), overrides=overrides)
        assert not ({op.line_key for op in imp.diff.ops} & edited), seed
        for key in edited:
            row = row_of(imp.diff.next_lines, key)
            assert (row.state, row.tag) == ("active", "business"), seed
            assert row == row_of(prev, key), seed


class TestEarlierOwnerChoice:
    def personal_choice(self):
        first = october_import()
        key = first.key(CLAUDE)
        chosen = run_import(
            fixture()["october"],
            rules=fixture_rules(),
            overrides={key: OwnerOverride(tag="personal")},
        )
        return key, commit_rows(chosen.diff)

    def test_the_earlier_owner_tag_is_kept_when_the_assistant_and_rules_now_disagree(self):
        key, prev = self.personal_choice()
        imp = october_import(previous=prev)  # no override this time; the rule says business
        entry = entry_of(imp.diff, key)
        assert entry.action == "kept_earlier_choice" and entry.kept_fields == ("tag",)
        assert "kept_earlier_choice" in entry.flags
        assert imp.diff.ops == () and imp.diff.line_writes == ()
        assert row_of(imp.diff.next_lines, key).tag == "personal"
        assert imp.diff.is_noop

    def test_a_new_override_in_this_review_beats_the_earlier_choice(self):
        key, prev = self.personal_choice()
        imp = october_import(previous=prev, overrides={key: OwnerOverride(tag="business")})
        entry = entry_of(imp.diff, key)
        assert (entry.action, entry.reason) == ("add", "became_business")
        assert [op.op for op in imp.diff.ops] == ["create"]
        assert row_of(imp.diff.next_lines, key).owner_override.tag == "business"

    def test_when_the_assistant_agrees_with_the_earlier_choice_it_is_a_plain_keep(self):
        first = october_import()
        key = first.key(SUPERMARKET)  # personal by rule
        chosen = october_import(overrides={key: OwnerOverride(tag="personal")})
        prev = commit_rows(chosen.diff)
        again = october_import(previous=prev)
        assert entry_of(again.diff, key).action == "keep"

    def test_earlier_category_and_subscription_choices_are_kept_too(self):
        first = october_import()
        key = first.key(OFFICE)
        chosen = october_import(
            overrides={
                key: OwnerOverride(category="מבצע מיוחד", path="לימוד", is_subscription=True)
            }
        )
        prev = commit_rows(chosen.diff)
        again = october_import(previous=prev)
        entry = entry_of(again.diff, key)
        assert entry.action == "kept_earlier_choice"
        assert set(entry.kept_fields) == {"category", "path", "is_subscription"}
        assert again.diff.is_noop
        row = row_of(again.diff.next_lines, key)
        assert (row.classification.category, row.classification.path) == ("מבצע מיוחד", "לימוד")

    def test_a_missing_assistant_tag_does_not_void_a_committed_line(self):
        first, prev = committed_october()
        raw = fixture()["october"]
        raw["lines"] = [
            {**x, "suggested_tag": None} if x["merchant_display"] == OFFICE else x
            for x in raw["lines"]
        ]
        imp = run_import(raw, previous=prev, rules=fixture_rules())
        key = first.key(OFFICE)
        assert imp.diff.ops == ()
        assert row_of(imp.diff.next_lines, key).tag == "business"
        assert imp.diff.blocking == ()

    def test_the_earlier_choice_survives_personal_minimisation(self):
        key, prev = self.personal_choice()
        row = row_of(prev, key)
        assert row.facts is None and row.owner_override == OwnerOverride(tag="personal")


class TestMergeWithHandEnteredExpenses:
    def test_exactly_one_hand_entered_match_is_a_link_and_creates_no_expense(self):
        imp = october_import(candidates=fixture_manual_expenses())
        key = imp.key(CLAUDE)
        entry = entry_of(imp.diff, key)
        assert (entry.action, entry.reason) == ("add", "linked_to_manual")
        links = ops_of(imp.diff, "link")
        assert [(op.line_key, op.expense_id) for op in links] == [(key, "exp-manual-claude")]
        assert key not in {op.line_key for op in ops_of(imp.diff, "create")}
        row = row_of(imp.diff.next_lines, key)
        assert row.matched_expense_id == "exp-manual-claude" and row.expense_id is None
        assert row.matched_source_key == "manual:claude-sep"

    def test_several_candidates_flag_the_line_and_do_not_merge(self):
        imp = october_import(candidates=fixture_manual_expenses())
        coffee_keys = {imp.key(COFFEE, 0), imp.key(COFFEE, 1)}
        assert imp.diff.blocking == ("merge_choice_required",)
        for key in coffee_keys:
            entry = entry_of(imp.diff, key)
            assert "merge_choice_required" in entry.flags
            assert entry.candidate_expense_ids == ("exp-manual-coffee-a", "exp-manual-coffee-b")
        touched = {op.line_key for op in imp.diff.ops}
        assert not (touched & coffee_keys)

    def test_choices_resolve_the_flag(self):
        base = october_import(candidates=fixture_manual_expenses())
        k0, k1 = base.key(COFFEE, 0), base.key(COFFEE, 1)
        imp = october_import(
            candidates=fixture_manual_expenses(),
            choices=ReviewChoices(merge={k0: "exp-manual-coffee-a", k1: "exp-manual-coffee-b"}),
        )
        assert imp.diff.blocking == ()
        links = {op.line_key: op.expense_id for op in ops_of(imp.diff, "link")}
        assert links[k0] == "exp-manual-coffee-a" and links[k1] == "exp-manual-coffee-b"

    def test_choosing_none_creates_a_new_expense_instead_of_merging(self):
        base = october_import(candidates=fixture_manual_expenses())
        key = base.key(CLAUDE)
        imp = october_import(
            candidates=fixture_manual_expenses(), choices=ReviewChoices(merge={key: None})
        )
        assert key in {op.line_key for op in ops_of(imp.diff, "create")}
        assert key not in {op.line_key for op in ops_of(imp.diff, "link")}

    def test_a_choice_must_be_one_of_the_candidates(self):
        base = october_import(candidates=fixture_manual_expenses())
        key = base.key(CLAUDE)
        with pytest.raises(StatementValidationError) as raised:
            october_import(
                candidates=fixture_manual_expenses(),
                choices=ReviewChoices(merge={key: "exp-cash-tip"}),
            )
        assert raised.value.issues[0].code == "merge_choice_invalid"

    def test_two_lines_cannot_link_the_same_expense(self):
        base = october_import(candidates=fixture_manual_expenses())
        k0, k1 = base.key(COFFEE, 0), base.key(COFFEE, 1)
        with pytest.raises(StatementValidationError) as raised:
            october_import(
                candidates=fixture_manual_expenses(),
                choices=ReviewChoices(merge={k0: "exp-manual-coffee-a", k1: "exp-manual-coffee-a"}),
            )
        assert raised.value.issues[0].code == "merge_choice_conflict"

    def test_a_link_carries_over_by_line_key(self):
        first = october_import(candidates=fixture_manual_expenses(), choices=_coffee_choices())
        prev = commit_rows(first.diff)
        again = october_import(previous=prev, candidates=fixture_manual_expenses())
        assert again.diff.is_noop

    def test_if_the_linked_line_disappears_the_link_goes_and_the_hand_entry_stays(self):
        first = october_import(candidates=fixture_manual_expenses(), choices=_coffee_choices())
        prev = commit_rows(first.diff)
        key = first.key(CLAUDE)
        raw = fixture()["october"]
        raw["lines"] = without(raw["lines"], CLAUDE)
        imp = run_import(
            raw,
            previous=prev,
            rules=fixture_rules(),
            candidates=fixture_manual_expenses(),
        )
        [op] = [op for op in imp.diff.ops if op.line_key == key]
        assert (op.op, op.expense_id) == ("unlink", "exp-manual-claude")
        assert not [x for x in imp.diff.ops if x.op == "void" and x.line_key == key]
        assert row_of(imp.diff.next_lines, key).state == "replaced"

    def test_a_linked_line_that_becomes_personal_unlinks_instead_of_voiding(self):
        first = october_import(candidates=fixture_manual_expenses(), choices=_coffee_choices())
        prev = commit_rows(first.diff)
        key = first.key(CLAUDE)
        imp = october_import(
            previous=prev,
            candidates=fixture_manual_expenses(),
            overrides={key: OwnerOverride(tag="personal")},
        )
        assert [(op.op, op.expense_id) for op in imp.diff.ops] == [("unlink", "exp-manual-claude")]

    def test_choosing_none_on_a_linked_line_unlinks_it_and_creates_an_expense(self):
        first = october_import(candidates=fixture_manual_expenses(), choices=_coffee_choices())
        prev = commit_rows(first.diff)
        key = first.key(CLAUDE)
        imp = october_import(
            previous=prev,
            candidates=fixture_manual_expenses(),
            choices=ReviewChoices(merge={key: None}),
        )
        assert [(op.op, op.line_key) for op in imp.diff.ops] == [("unlink", key), ("create", key)]
        assert imp.diff.ops[0].expense_id == "exp-manual-claude"
        row = row_of(imp.diff.next_lines, key)
        assert row.matched_expense_id is None and row.matched_source_key is None

    def test_naming_an_expense_for_an_already_linked_line_is_refused(self):
        first = october_import(candidates=fixture_manual_expenses(), choices=_coffee_choices())
        prev = commit_rows(first.diff)
        key = first.key(CLAUDE)
        with pytest.raises(StatementValidationError) as raised:
            october_import(
                previous=prev,
                candidates=fixture_manual_expenses(),
                choices=ReviewChoices(merge={key: "exp-manual-claude"}),
            )
        assert raised.value.issues[0].code == "merge_choice_invalid"

    def test_an_expense_freed_by_a_replaced_line_can_be_linked_by_a_new_line_in_the_same_diff(self):
        first = october_import(candidates=fixture_manual_expenses(), choices=_coffee_choices())
        prev = commit_rows(first.diff)
        old_key = first.key(CLAUDE)
        raw = fixture()["october"]
        raw["lines"] = [
            {**x, "amount_minor": 7600} if x["merchant_display"] == CLAUDE else x
            for x in raw["lines"]
        ]
        imp = run_import(
            raw, previous=prev, rules=fixture_rules(), candidates=fixture_manual_expenses()
        )
        new_key = next(
            e.line_key for e in imp.diff.entries if e.action == "add" and e.line_key != old_key
        )
        # the unlink is listed before the link so a unique "one line per hand entry" index holds
        assert [(op.op, op.line_key, op.expense_id) for op in imp.diff.ops] == [
            ("unlink", old_key, "exp-manual-claude"),
            ("link", new_key, "exp-manual-claude"),
        ]

    def test_cash_and_hand_entered_expenses_are_never_modified(self):
        imp = october_import(candidates=fixture_manual_expenses(), choices=_coffee_choices())
        sid = imp.batch.statement_id
        manual_ids = {e.expense_id for e in fixture_manual_expenses()}
        for op in imp.diff.ops:
            if op.op in {"create", "update", "revive", "void"}:
                assert owns_source_key(sid, op.source_key)
                assert op.expense_id not in manual_ids
            else:
                assert op.op == "link" and op.expense_id in manual_ids

    def test_a_linked_credit_is_never_attempted(self):
        imp = october_import(candidates=fixture_manual_expenses())
        assert imp.key("OFFICE SUPPLY DEMO RETURN") not in {
            op.line_key for op in ops_of(imp.diff, "link")
        }


def _coffee_choices():
    base = october_import()
    return ReviewChoices(
        merge={
            base.key(COFFEE, 0): "exp-manual-coffee-a",
            base.key(COFFEE, 1): "exp-manual-coffee-b",
        }
    )


class TestOnlyThisStatementsExpensesAreTouched:
    def test_a_stored_line_pointing_at_a_hand_entered_expense_is_refused(self):
        first, prev = committed_october()
        key = first.key(CLAUDE)
        poisoned = [
            replace(r, expense_id="exp-manual-claude", expense_source_key="manual:claude-sep")
            if r.line_key == key
            else r
            for r in prev
        ]
        raw = fixture()["october"]
        raw["lines"] = without(raw["lines"], CLAUDE)
        with pytest.raises(StatementValidationError) as raised:
            run_import(raw, previous=poisoned, rules=fixture_rules())
        assert raised.value.issues[0].code == "foreign_expense_reference"

    def test_a_stored_line_pointing_at_another_statements_expense_is_refused(self):
        first, prev = committed_october()
        key = first.key(CLAUDE)
        other = statement_source_key("STM_" + "9" * 32, key)
        poisoned = [replace(r, expense_source_key=other) if r.line_key == key else r for r in prev]
        with pytest.raises(StatementValidationError) as raised:
            october_import(previous=poisoned)
        assert raised.value.issues[0].code == "foreign_expense_reference"

    def test_a_stored_expense_reference_without_a_source_key_is_refused(self):
        first, prev = committed_october()
        key = first.key(CLAUDE)
        poisoned = [replace(r, expense_source_key=None) if r.line_key == key else r for r in prev]
        with pytest.raises(StatementValidationError):
            october_import(previous=poisoned)

    def test_a_stored_line_of_another_statement_is_refused(self):
        first, prev = committed_october()
        poisoned = [replace(prev[0], statement_id="STM_" + "9" * 32), *prev[1:]]
        with pytest.raises(StatementValidationError) as raised:
            october_import(previous=poisoned)
        assert raised.value.issues[0].code == "foreign_statement_line"

    def test_a_link_to_a_statement_created_expense_is_refused(self):
        imp = october_import(candidates=fixture_manual_expenses())
        key = imp.key(CLAUDE)
        prev = commit_rows(imp.diff)
        poisoned = [
            replace(r, matched_source_key="stmt:STM_" + "9" * 32 + ":" + "a" * 64)
            if r.line_key == key
            else r
            for r in prev
        ]
        with pytest.raises(StatementValidationError):
            october_import(previous=poisoned)

    def test_duplicate_stored_or_incoming_lines_are_refused(self):
        first, prev = committed_october()
        with pytest.raises(StatementValidationError):
            october_import(previous=[*prev, prev[0]])

    def test_a_batch_parsed_for_another_statement_is_refused(self):
        # otherwise every earlier line would look missing and be voided
        first, prev = committed_october()
        november = parse(fixture()["november"])
        with pytest.raises(StatementValidationError) as raised:
            diff_statement(
                tuple(prev),
                apply_rules(november.lines, fixture_rules()),
                statement_id=first.batch.statement_id,
                charge_date=first.batch.charge_date,
            )
        assert raised.value.issues[0].code == "foreign_batch_line"

    def test_an_empty_import_is_refused_instead_of_voiding_the_statement(self):
        first, prev = committed_october()
        with pytest.raises(StatementValidationError) as raised:
            diff_statement(
                tuple(prev),
                (),
                statement_id=first.batch.statement_id,
                charge_date=first.batch.charge_date,
            )
        assert raised.value.issues[0].code == "empty_import"

    @pytest.mark.parametrize("seed", range(20))
    def test_property_every_mutating_op_targets_this_statement_only(self, seed):
        rnd = random.Random(seed)
        raw = random_statement(seed)
        first = run_import(raw, candidates=_noise_candidates(seed))
        prev = commit_rows(first.diff)
        lines = list(raw["lines"])
        for _ in range(rnd.randint(0, 4)):
            if len(lines) > 1:
                lines.pop(rnd.randrange(len(lines)))
        lines += [raw_line(merchant_display="NEWCOMER", amount_minor=rnd.randint(1, 5000))]
        batch_keys = [x.line_key for x in run_import(raw_batch(lines)).batch.lines]
        overrides = {
            key: OwnerOverride(tag=rnd.choice(["business", "personal"]))
            for key in rnd.sample(batch_keys, min(5, len(batch_keys)))
        }
        imp = run_import(
            raw_batch(lines), previous=prev, overrides=overrides, candidates=_noise_candidates(seed)
        )
        sid = imp.batch.statement_id
        for op in imp.diff.ops:
            if op.op in {"create", "update", "revive", "void"}:
                assert owns_source_key(sid, op.source_key), (seed, op)
            else:
                assert op.op in {"link", "unlink"}
        assert all(r.statement_id == sid for r in imp.diff.next_lines)


def _noise_candidates(seed):
    from tests.unit.statements.helpers import manual_expense

    return [
        manual_expense(
            expense_id=f"noise-{seed}-{n}",
            source_key=f"manual:noise-{n}",
            date=f"2026-09-{20 + n}",
            vendor="ALPHA TOOLS",
            amount_minor=1000,
            currency="ILS",
            payment_status="שולם",
        )
        for n in range(3)
    ]


class TestPersonalMinimisation:
    def test_personal_rows_hold_no_amount_merchant_or_date(self):
        first, prev = committed_october()
        personal = [r for r in prev if r.tag == "personal"]
        assert len(personal) == 3
        for row in personal:
            assert row.facts is None and row.classification is None
            assert row.rule_id is None and row.matched_expense_id is None
            assert row.state == "active" and len(row.line_key) == 64

    def test_no_personal_text_survives_anywhere_in_the_stored_rows(self):
        first, prev = committed_october()
        personal_text = (
            "SUPERMARKET DEMO",
            "PHARMACY DEMO",
            "מכולת לדוגמה",
            "SUPERMARKET",
            "PHARMACY",
        )
        for row in prev:
            if row.tag == "personal":
                text = repr(row)
                assert not any(piece in text for piece in personal_text), text

    def test_flipping_to_personal_leaves_no_detail_behind(self):
        first, prev = committed_october()
        key = first.key(CLAUDE)
        imp = october_import(previous=prev, overrides={key: OwnerOverride(tag="personal")})
        row = row_of(imp.diff.next_lines, key)
        assert row.facts is None and row.classification is None
        assert "CLAUDE" not in repr(row)

    @pytest.mark.parametrize("seed", range(25))
    def test_property_personal_rows_are_always_key_only(self, seed):
        raw = random_statement(seed)
        imp = run_import(raw)
        for row in imp.diff.next_lines:
            if row.tag == "personal":
                assert row.facts is None and row.classification is None, seed

    def test_minimisation_can_be_switched_off_so_a_personal_total_exists(self):
        imp = october_import(minimize_personal=False)
        personal = [r for r in imp.diff.next_lines if r.tag == "personal"]
        assert personal and all(r.facts is not None for r in personal)
        assert all(r.classification is not None for r in personal)
        assert imp.diff.ops == october_import().diff.ops  # personal still never touches expenses

    def test_switching_minimisation_on_later_strips_existing_detail(self):
        full = october_import(minimize_personal=False)
        prev = commit_rows(full.diff)
        minimized = october_import(previous=prev, minimize_personal=True)
        stripped = [r for r in minimized.diff.next_lines if r.tag == "personal"]
        assert stripped and all(r.facts is None for r in stripped)
        assert minimized.diff.ops == ()


class TestDiffSurface:
    def test_the_diff_is_deterministic_and_input_order_independent(self):
        lines = list(fixture()["october"]["lines"])
        random.Random(3).shuffle(lines)
        a = october_import()
        b = run_import(raw_batch(lines), rules=fixture_rules())
        assert a.diff == b.diff

    def test_counts_support_the_replace_banner(self):
        first, prev = committed_october()
        raw = fixture()["october"]
        raw["lines"] = without(raw["lines"], CLAUDE)
        imp = run_import(raw, previous=prev, rules=fixture_rules())
        assert imp.diff.counts["void"] == 1 and imp.diff.counts["keep"] == 11
        merged = october_import(candidates=fixture_manual_expenses()).diff
        assert merged.merged_count == 1

    def test_the_inputs_are_never_mutated(self):
        first, prev = committed_october()
        snapshot = list(prev)
        october_import(previous=prev)
        assert prev == snapshot
