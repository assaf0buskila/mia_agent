"""Merchant rules: precedence, disagreement flags, owner overrides, learning as data only."""

from __future__ import annotations

from dataclasses import replace

import pytest
from app.domain.statements import (
    DEFAULT_CATEGORY,
    DEFAULT_PATH,
    DEFAULT_PLATFORM,
    LOW_CONFIDENCE,
    MerchantRule,
    OwnerOverride,
    StatementValidationError,
    apply_rules,
    best_rule,
    confirmations_from_review,
    learn_rules,
    rule_matches,
)

from tests.unit.statements.helpers import fixture, fixture_rules, parse, raw_batch, raw_line, rule


def resolve(lines, rules=(), overrides=None):
    batch = parse(raw_batch(lines))
    return batch, apply_rules(batch.lines, list(rules), overrides)


class TestRuleData:
    def test_fixture_rules_load(self):
        assert [r.rule_id for r in fixture_rules()][:2] == ["rule_claude", "rule_chatgpt"]

    @pytest.mark.parametrize(
        "fields",
        [
            {"merchant_key": "Claude"},  # not normalized
            {"merchant_key": ""},
            {"merchant_key": "ab", "match": "prefix"},  # too short to be a safe prefix
            {"tag": "maybe"},
            {"match": "regex"},
            {"path": "elsewhere"},
            {"platform": "TikTok"},
            {"category": "x" * 41},
            {"label": "x" * 81},
            {"aliases": ("Not Normalized",)},
            {"category": "card 12345678"},
            {"revision": 0},
            {"is_subscription": "yes"},
            {"rule_id": ""},
        ],
    )
    def test_invalid_rule_data_is_rejected(self, fields):
        base = {"rule_id": "r1", "merchant_key": "claude", "tag": "business"}
        base.update(fields)
        with pytest.raises(StatementValidationError):
            MerchantRule(**base)

    def test_rules_are_immutable(self):
        with pytest.raises(AttributeError):
            rule(rule_id="r1", merchant_key="claude", tag="business").tag = "personal"  # type: ignore[misc]


class TestMatchingAndPrecedence:
    def test_exact_and_prefix_matching(self):
        exact = rule(rule_id="e", merchant_key="capcut", tag="business")
        prefix = rule(rule_id="p", merchant_key="openai", match="prefix", tag="business")
        assert rule_matches(exact, "capcut") and not rule_matches(exact, "capcut pro")
        assert rule_matches(prefix, "openai chatgpt") and rule_matches(prefix, "openai")
        assert not rule_matches(prefix, "xopenai")

    def test_alias_matches_like_the_primary_key(self):
        claude = rule(rule_id="c", merchant_key="claude ai", tag="business", aliases=["anthropic"])
        assert rule_matches(claude, "anthropic")
        assert not rule_matches(claude, "anthropic pbc")

    def test_exact_beats_prefix_and_longer_prefix_beats_shorter(self):
        rules = [
            rule(rule_id="short", merchant_key="open", match="prefix", tag="personal"),
            rule(rule_id="long", merchant_key="openai", match="prefix", tag="business"),
            rule(rule_id="exact", merchant_key="openai chatgpt", tag="personal"),
        ]
        assert best_rule(rules, "openai chatgpt").rule.rule_id == "exact"
        assert best_rule(rules, "openai labs").rule.rule_id == "long"
        assert best_rule(rules, "opener").rule.rule_id == "short"
        assert best_rule(rules, "nothing") is None

    def test_an_exact_rule_beats_a_prefix_rule_on_the_same_text_even_with_an_older_revision(self):
        rules = [
            rule(rule_id="p", merchant_key="capcut", match="prefix", tag="personal", revision=5),
            rule(rule_id="e", merchant_key="capcut", tag="business", revision=1),
        ]
        match = best_rule(rules, "capcut")
        assert match.rule.rule_id == "e" and match.conflict is False

    def test_inactive_rules_never_match(self):
        rules = [rule(rule_id="off", merchant_key="capcut", tag="business", active=False)]
        assert best_rule(rules, "capcut") is None

    def test_same_tier_disagreement_is_a_conflict_and_the_newer_revision_wins(self):
        rules = [
            rule(rule_id="old", merchant_key="capcut", tag="personal", revision=1),
            rule(rule_id="new", merchant_key="capcut", tag="business", revision=2),
        ]
        match = best_rule(rules, "capcut")
        assert match.rule.rule_id == "new" and match.conflict is True

    def test_a_less_specific_disagreeing_rule_is_not_a_conflict(self):
        rules = [
            rule(rule_id="p", merchant_key="open", match="prefix", tag="personal"),
            rule(rule_id="e", merchant_key="openai", tag="business"),
        ]
        assert best_rule(rules, "openai").conflict is False


class TestApplyRules:
    def test_rule_wins_over_the_assistant_and_the_disagreement_is_flagged(self):
        _, [resolved] = resolve(
            [raw_line(merchant_display="SUPERMARKET DEMO", suggested_tag="business")],
            fixture_rules(),
        )
        assert resolved.tag == "personal" and resolved.tag_source == "rule"
        assert resolved.rule_id == "rule_supermarket"
        assert "rule_disagreement" in resolved.flags and resolved.disagreements == ("tag",)

    def test_agreement_raises_no_flag(self):
        _, [resolved] = resolve(
            [raw_line(merchant_display="SUPERMARKET DEMO", suggested_tag="personal")],
            fixture_rules(),
        )
        assert resolved.flags == () and resolved.disagreements == ()

    def test_rule_fields_beat_the_suggestion(self):
        _, [resolved] = resolve(
            [
                raw_line(
                    merchant_display="META ADS DEMO",
                    suggested_category="other",
                    suggested_path="לימוד",
                )
            ],
            fixture_rules(),
        )
        assert (resolved.category, resolved.path, resolved.platform) == (
            "פרסום",
            "אוטומציה",
            "Meta",
        )
        assert set(resolved.disagreements) == {"category", "path"}

    def test_assistant_decides_when_no_rule_matches(self):
        _, [resolved] = resolve(
            [raw_line(suggested_category="ציוד", suggested_path="לימוד", is_subscription=True)]
        )
        assert (resolved.tag, resolved.tag_source) == ("business", "assistant")
        assert (resolved.category, resolved.path, resolved.is_subscription) == (
            "ציוד",
            "לימוד",
            True,
        )
        assert resolved.rule_id is None

    def test_defaults_when_nobody_classifies(self):
        _, [resolved] = resolve([raw_line()])
        assert (resolved.category, resolved.path, resolved.platform, resolved.is_subscription) == (
            DEFAULT_CATEGORY,
            DEFAULT_PATH,
            DEFAULT_PLATFORM,
            False,
        )

    def test_owner_override_beats_rule_and_assistant(self):
        batch = parse(raw_batch([raw_line(merchant_display="SUPERMARKET DEMO")]))
        key = batch.lines[0].line_key
        [resolved] = apply_rules(
            batch.lines,
            fixture_rules(),
            {key: OwnerOverride(tag="business", category="אירוח", path="לימוד")},
        )
        assert (resolved.tag, resolved.tag_source) == ("business", "owner_review")
        assert (resolved.category, resolved.path) == ("אירוח", "לימוד")

    def test_override_of_subscription_false_beats_a_true_rule(self):
        batch = parse(raw_batch([raw_line(merchant_display="CAPCUT")]))
        key = batch.lines[0].line_key
        [resolved] = apply_rules(
            batch.lines, fixture_rules(), {key: OwnerOverride(is_subscription=False)}
        )
        assert resolved.is_subscription is False and resolved.tag == "business"

    def test_unassigned_when_nobody_decides(self):
        _, [resolved] = resolve([raw_line(suggested_tag=None)])
        assert resolved.tag is None and resolved.tag_source == "unassigned"
        assert "unassigned" in resolved.flags

    def test_low_confidence_is_flagged_only_on_assistant_decisions(self):
        low = LOW_CONFIDENCE - 0.01
        _, [assistant_only] = resolve([raw_line(confidence=low)])
        assert "low_confidence" in assistant_only.flags
        _, [at_threshold] = resolve([raw_line(confidence=LOW_CONFIDENCE)])
        assert "low_confidence" not in at_threshold.flags
        _, [with_rule] = resolve(
            [raw_line(merchant_display="CAPCUT", confidence=low)], fixture_rules()
        )
        assert "low_confidence" not in with_rule.flags

    def test_conflicting_rules_are_flagged(self):
        rules = [
            rule(rule_id="a", merchant_key="demo shop", tag="personal"),
            rule(rule_id="b", merchant_key="demo shop", tag="business", revision=2),
        ]
        _, [resolved] = resolve([raw_line()], rules)
        assert resolved.tag == "business" and "rule_conflict" in resolved.flags

    def test_an_override_for_a_line_that_is_not_in_the_batch_is_rejected(self):
        batch = parse(raw_batch([raw_line()]))
        with pytest.raises(StatementValidationError) as raised:
            apply_rules(batch.lines, [], {"f" * 64: OwnerOverride(tag="personal")})
        assert raised.value.issues[0].code == "override_unknown_line"

    def test_duplicate_rule_ids_are_rejected(self):
        batch = parse(raw_batch([raw_line()]))
        rules = [
            rule(rule_id="same", merchant_key="a shop", tag="business"),
            rule(rule_id="same", merchant_key="b shop", tag="business"),
        ]
        with pytest.raises(StatementValidationError):
            apply_rules(batch.lines, rules)

    def test_output_order_follows_the_canonical_batch_order(self):
        batch, resolved = resolve(fixture()["october"]["lines"], fixture_rules())
        assert [x.line.line_key for x in resolved] == [x.line_key for x in batch.lines]


class TestOwnerOverrideData:
    @pytest.mark.parametrize(
        "fields",
        [{"tag": "maybe"}, {"path": "x"}, {"category": "x" * 41}, {"is_subscription": 1}],
    )
    def test_invalid_override_is_rejected(self, fields):
        with pytest.raises(StatementValidationError):
            OwnerOverride(**fields)


class TestLearningAsData:
    def review(self, **override_fields):
        batch = parse(raw_batch([raw_line(merchant_display="Demo  SHOP")]))
        key = batch.lines[0].line_key
        return apply_rules(batch.lines, [], {key: OwnerOverride(**override_fields)})

    def test_assistant_suggestions_alone_never_produce_a_rule(self):
        _, resolved = resolve(fixture()["october"]["lines"])
        assert confirmations_from_review(resolved) == ()

    def test_an_override_without_make_rule_produces_no_rule(self):
        assert confirmations_from_review(self.review(tag="personal")) == ()

    def test_always_for_this_merchant_becomes_an_exact_rule_proposal(self):
        resolved = self.review(tag="personal", make_rule=True)
        [confirmation] = confirmations_from_review(resolved)
        assert (confirmation.merchant_key, confirmation.tag) == ("demo shop", "personal")
        [proposal] = learn_rules([confirmation], [])
        assert proposal.action == "insert" and proposal.previous is None
        learned = proposal.rule
        assert (learned.match, learned.tag, learned.revision, learned.active) == (
            "exact",
            "personal",
            1,
            True,
        )
        assert learned.merchant_key == "demo shop" and learned.rule_id.startswith("MR_")

    def test_the_learned_rule_is_applied_on_the_next_import(self):
        [confirmation] = confirmations_from_review(
            self.review(tag="business", category="ציוד", is_subscription=True, make_rule=True)
        )
        [proposal] = learn_rules([confirmation], [])
        _, [resolved] = resolve(
            [raw_line(merchant_display="DEMO SHOP", suggested_tag="personal")], [proposal.rule]
        )
        assert (resolved.tag, resolved.category, resolved.is_subscription) == (
            "business",
            "ציוד",
            True,
        )
        assert "rule_disagreement" in resolved.flags

    def test_an_existing_exact_rule_is_updated_with_the_next_revision(self):
        existing = rule(
            rule_id="MR_old", merchant_key="demo shop", tag="business", revision=3, label="Demo"
        )
        [confirmation] = confirmations_from_review(self.review(tag="personal", make_rule=True))
        [proposal] = learn_rules([confirmation], [existing])
        assert proposal.action == "update" and proposal.previous == existing
        assert (proposal.rule.rule_id, proposal.rule.revision, proposal.rule.tag) == (
            "MR_old",
            4,
            "personal",
        )
        assert proposal.rule.label == "Demo"

    def test_an_identical_confirmation_is_a_noop(self):
        [confirmation] = confirmations_from_review(self.review(tag="personal", make_rule=True))
        [first] = learn_rules([confirmation], [])
        [again] = learn_rules([confirmation], [first.rule])
        assert again.action == "noop" and again.rule == first.rule

    def test_an_inactive_exact_rule_is_reactivated_by_an_update(self):
        [confirmation] = confirmations_from_review(self.review(tag="personal", make_rule=True))
        [first] = learn_rules([confirmation], [])
        [again] = learn_rules([confirmation], [replace(first.rule, active=False)])
        assert again.action == "update" and again.rule.active is True and again.rule.revision == 2

    def test_a_broader_prefix_rule_does_not_block_an_exact_insert(self):
        prefix = rule(rule_id="p", merchant_key="demo", match="prefix", tag="business")
        [confirmation] = confirmations_from_review(self.review(tag="personal", make_rule=True))
        [proposal] = learn_rules([confirmation], [prefix])
        assert proposal.action == "insert" and proposal.rule.match == "exact"

    def test_conflicting_confirmations_for_one_merchant_are_rejected(self):
        batch = parse(
            raw_batch(
                [
                    raw_line(merchant_display="DEMO SHOP", amount_minor=100),
                    raw_line(merchant_display="demo shop", amount_minor=200),
                ]
            )
        )
        a, b = (line.line_key for line in batch.lines)
        resolved = apply_rules(
            batch.lines,
            [],
            {
                a: OwnerOverride(tag="personal", make_rule=True),
                b: OwnerOverride(tag="business", make_rule=True),
            },
        )
        with pytest.raises(StatementValidationError) as raised:
            confirmations_from_review(resolved)
        assert raised.value.issues[0].code == "rule_confirmation_conflict"

    def test_agreeing_duplicates_collapse_to_one_confirmation(self):
        batch = parse(
            raw_batch(
                [
                    raw_line(merchant_display="DEMO SHOP", amount_minor=100),
                    raw_line(merchant_display="demo shop", amount_minor=200),
                ]
            )
        )
        overrides = {x.line_key: OwnerOverride(tag="personal", make_rule=True) for x in batch.lines}
        assert len(confirmations_from_review(apply_rules(batch.lines, [], overrides))) == 1

    def test_make_rule_needs_a_decided_tag(self):
        batch = parse(raw_batch([raw_line(suggested_tag=None)]))
        key = batch.lines[0].line_key
        resolved = apply_rules(batch.lines, [], {key: OwnerOverride(make_rule=True)})
        with pytest.raises(StatementValidationError) as raised:
            confirmations_from_review(resolved)
        assert raised.value.issues[0].code == "rule_without_tag"

    def test_make_rule_needs_the_tag_from_the_owner_not_the_assistant(self):
        line = raw_line(merchant_display="Demo  SHOP", suggested_tag="business")
        batch = parse(raw_batch([line]))
        key = batch.lines[0].line_key
        resolved = apply_rules(batch.lines, [], {key: OwnerOverride(make_rule=True)})
        assert resolved[0].tag == "business" and resolved[0].tag_source == "assistant"
        with pytest.raises(StatementValidationError) as raised:
            confirmations_from_review(resolved)
        assert raised.value.issues[0].code == "rule_without_owner_tag"

    def test_make_rule_does_not_take_the_tag_from_an_approved_rule_either(self):
        line = raw_line(merchant_display="Demo  SHOP", suggested_tag="business")
        batch = parse(raw_batch([line]))
        key = batch.lines[0].line_key
        approved = [rule(rule_id="r1", merchant_key="demo shop", tag="personal")]
        resolved = apply_rules(batch.lines, approved, {key: OwnerOverride(make_rule=True)})
        assert resolved[0].tag == "personal" and resolved[0].tag_source == "rule"
        with pytest.raises(StatementValidationError) as raised:
            confirmations_from_review(resolved)
        assert raised.value.issues[0].code == "rule_without_owner_tag"

    def test_learning_never_mutates_what_it_is_given(self):
        existing = [rule(rule_id="MR_old", merchant_key="demo shop", tag="business")]
        snapshot = list(existing)
        [confirmation] = confirmations_from_review(self.review(tag="personal", make_rule=True))
        learn_rules([confirmation], existing)
        assert existing == snapshot
