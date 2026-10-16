"""Merchant business/personal rules, precedence, and learning from owner confirmations.

Precedence for every classified field, strongest first:

    owner review (this session) > approved merchant rule > assistant suggestion > default

Rules are data. The assistant can never write one: `learn_rules` only turns an explicit owner
confirmation ("always for this merchant") into a proposed rule row, which the caller still has
to commit through the owner's approval.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from .batch import BatchLine
from .errors import IssueCollector, StatementValidationError
from .text import check_text, normalize_merchant
from .vocabulary import (
    DEFAULT_CATEGORY,
    DEFAULT_PATH,
    DEFAULT_PLATFORM,
    LOW_CONFIDENCE,
    MATCH_KINDS,
    MAX_ALIASES,
    MAX_CATEGORY_CHARS,
    MAX_KEY_CHARS,
    MAX_RULE_LABEL_CHARS,
    MIN_PREFIX_CHARS,
    PATHS,
    PLATFORMS,
    TAGS,
)

_MAX_RULE_ID_CHARS = 64


def _invalid(field: str, code: str = "invalid_rule") -> StatementValidationError:
    return StatementValidationError.of(code, field)


def _check_label_text(value: object, field: str, max_chars: int) -> None:
    issues = IssueCollector()
    if check_text(value, path=field, max_chars=max_chars, issues=issues) != value:
        issues.add(field, "not_normalized")
    issues.raise_if_any()


@dataclass(frozen=True)
class OwnerOverride:
    """What the owner chose in review. A None field means he did not choose it.

    `make_rule` ("always for this merchant") needs the tag in the same override: the rule takes
    the tag he states, never the one a rule or the assistant suggested, and never one the diff
    kept from an earlier review. A client that shows a tag sends that tag with the tap.
    """

    tag: str | None = None
    category: str | None = field(default=None, repr=False)
    path: str | None = None
    is_subscription: bool | None = None
    make_rule: bool = False

    def __post_init__(self) -> None:
        if self.tag is not None and self.tag not in TAGS:
            raise _invalid("tag", "invalid_tag")
        if self.path is not None and self.path not in PATHS:
            raise _invalid("path", "invalid_path")
        if self.is_subscription is not None and not isinstance(self.is_subscription, bool):
            raise _invalid("is_subscription", "invalid_flag")
        if not isinstance(self.make_rule, bool):
            raise _invalid("make_rule", "invalid_flag")
        if self.category is not None:
            _check_label_text(self.category, "category", MAX_CATEGORY_CHARS)

    @property
    def is_empty(self) -> bool:
        return (
            self.tag is None
            and self.category is None
            and self.path is None
            and self.is_subscription is None
        )


@dataclass(frozen=True)
class MerchantRule:
    """One approved rule row. `merchant_key` is a normalized key (see normalize_merchant)."""

    rule_id: str
    merchant_key: str = field(repr=False)
    tag: str
    match: str = "exact"
    category: str | None = field(default=None, repr=False)
    path: str | None = None
    platform: str | None = None
    is_subscription: bool | None = None
    label: str | None = field(default=None, repr=False)
    aliases: tuple[str, ...] = field(default=(), repr=False)
    active: bool = True
    revision: int = 1

    def __post_init__(self) -> None:
        if (
            not isinstance(self.rule_id, str)
            or not 0 < len(self.rule_id) <= _MAX_RULE_ID_CHARS
            or self.rule_id != self.rule_id.strip()
        ):
            raise _invalid("rule_id")
        if self.tag not in TAGS:
            raise _invalid("tag", "invalid_tag")
        if self.match not in MATCH_KINDS:
            raise _invalid("match", "invalid_match")
        if self.path is not None and self.path not in PATHS:
            raise _invalid("path", "invalid_path")
        if self.platform is not None and self.platform not in PLATFORMS:
            raise _invalid("platform", "invalid_platform")
        if self.is_subscription is not None and not isinstance(self.is_subscription, bool):
            raise _invalid("is_subscription", "invalid_flag")
        if not isinstance(self.active, bool):
            raise _invalid("active", "invalid_flag")
        if type(self.revision) is not int or self.revision < 1:
            raise _invalid("revision", "invalid_revision")
        if self.category is not None:
            _check_label_text(self.category, "category", MAX_CATEGORY_CHARS)
        if self.label is not None:
            _check_label_text(self.label, "label", MAX_RULE_LABEL_CHARS)
        if not isinstance(self.aliases, tuple) or len(self.aliases) > MAX_ALIASES:
            raise _invalid("aliases")
        for pattern in (self.merchant_key, *self.aliases):
            self._check_pattern(pattern)

    def _check_pattern(self, pattern: object) -> None:
        if (
            not isinstance(pattern, str)
            or not 0 < len(pattern) <= MAX_KEY_CHARS
            or pattern != normalize_merchant(pattern)
        ):
            raise _invalid("merchant_key", "not_normalized")
        if self.match == "prefix" and len(pattern) < MIN_PREFIX_CHARS:
            raise _invalid("merchant_key", "prefix_too_short")


@dataclass(frozen=True)
class RuleMatch:
    rule: MerchantRule
    conflict: bool


def _matched_length(rule: MerchantRule, merchant_key: str) -> int | None:
    best: int | None = None
    for pattern in (rule.merchant_key, *rule.aliases):
        hit = merchant_key == pattern if rule.match == "exact" else merchant_key.startswith(pattern)
        if hit and (best is None or len(pattern) > best):
            best = len(pattern)
    return best


def rule_matches(rule: MerchantRule, merchant_key: str) -> bool:
    """Text match only (key or an alias, exact or prefix). Whether the rule is active is the
    caller's concern; `best_rule` skips inactive rules."""
    return _matched_length(rule, merchant_key) is not None


def _effect(rule: MerchantRule) -> tuple[Any, ...]:
    return (rule.tag, rule.category, rule.path, rule.platform, rule.is_subscription)


def best_rule(rules: Sequence[MerchantRule], merchant_key: str) -> RuleMatch | None:
    """Exact beats prefix; a longer prefix beats a shorter one. Rules tied at the top that
    disagree are a conflict: the caller is told, and one of them still wins deterministically
    (highest revision, then rule_id). Revisions of different rules are not comparable, so that
    tie-break only keeps the result stable; it says nothing about which rule is right."""
    scored: list[tuple[tuple[int, int], MerchantRule]] = []
    for rule in rules:
        if not rule.active:
            continue
        length = _matched_length(rule, merchant_key)
        if length is None:
            continue
        scored.append(((1, 0) if rule.match == "exact" else (0, length), rule))
    if not scored:
        return None
    top = max(score for score, _ in scored)
    tied = [rule for score, rule in scored if score == top]
    winner = max(tied, key=lambda rule: (rule.revision, rule.rule_id))
    return RuleMatch(winner, len({_effect(rule) for rule in tied}) > 1)


@dataclass(frozen=True)
class ResolvedLine:
    """A batch line with its tag and classification settled, and why."""

    line: BatchLine
    tag: str | None
    tag_source: str  # owner_review | rule | assistant | unassigned
    category: str = field(repr=False)
    path: str
    platform: str
    is_subscription: bool
    rule_id: str | None
    flags: tuple[str, ...]
    disagreements: tuple[str, ...]
    override: OwnerOverride | None


def first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _resolve(
    line: BatchLine, rules: Sequence[MerchantRule], override: OwnerOverride | None
) -> ResolvedLine:
    match = best_rule(rules, line.facts.merchant_key)
    rule = match.rule if match else None
    owner = override or OwnerOverride()

    suggestions = {
        "tag": line.suggested_tag,
        "category": line.suggested_category,
        "path": line.suggested_path,
        "is_subscription": line.is_subscription,
    }
    from_rule = {
        "tag": rule.tag if rule else None,
        "category": rule.category if rule else None,
        "path": rule.path if rule else None,
        "is_subscription": rule.is_subscription if rule else None,
    }
    disagreements = tuple(
        name
        for name in ("tag", "category", "path", "is_subscription")
        if from_rule[name] is not None
        and suggestions[name] is not None
        and from_rule[name] != suggestions[name]
    )

    tag = first_not_none(owner.tag, from_rule["tag"], suggestions["tag"])
    if owner.tag is not None:
        tag_source = "owner_review"
    elif from_rule["tag"] is not None:
        tag_source = "rule"
    elif suggestions["tag"] is not None:
        tag_source = "assistant"
    else:
        tag_source = "unassigned"

    flags: list[str] = []
    if disagreements:
        flags.append("rule_disagreement")
    if match is not None and match.conflict:
        flags.append("rule_conflict")
    if tag is None:
        flags.append("unassigned")
    if tag_source == "assistant" and line.confidence < LOW_CONFIDENCE:
        flags.append("low_confidence")

    return ResolvedLine(
        line=line,
        tag=tag,
        tag_source=tag_source,
        category=first_not_none(owner.category, from_rule["category"], suggestions["category"])
        or DEFAULT_CATEGORY,
        path=first_not_none(owner.path, from_rule["path"], suggestions["path"]) or DEFAULT_PATH,
        platform=(rule.platform if rule and rule.platform else DEFAULT_PLATFORM),
        is_subscription=bool(
            first_not_none(
                owner.is_subscription, from_rule["is_subscription"], suggestions["is_subscription"]
            )
        ),
        rule_id=rule.rule_id if rule else None,
        flags=tuple(flags),
        disagreements=disagreements,
        override=override,
    )


def apply_rules(
    lines: Sequence[BatchLine],
    rules: Sequence[MerchantRule],
    overrides: Mapping[str, OwnerOverride] | None = None,
) -> tuple[ResolvedLine, ...]:
    """Settle every line's tag and classification. Output follows the batch's canonical order."""
    ids = [rule.rule_id for rule in rules]
    if len(set(ids)) != len(ids):
        raise StatementValidationError.of("duplicate_rule_id", "rules")
    chosen = dict(overrides or {})
    known = {line.line_key for line in lines}
    for key, value in chosen.items():
        if key not in known:
            raise StatementValidationError.of("override_unknown_line", "overrides")
        if not isinstance(value, OwnerOverride):
            raise StatementValidationError.of("override_invalid", "overrides")
    return tuple(_resolve(line, rules, chosen.get(line.line_key)) for line in lines)


@dataclass(frozen=True)
class RuleConfirmation:
    """The owner said 'always for this merchant'. Only what he decided is carried: the tag, and
    the category, path and subscription flag only if he set them himself."""

    merchant_key: str = field(repr=False)
    tag: str
    category: str | None = field(repr=False)
    path: str | None
    is_subscription: bool | None


def confirmations_from_review(
    resolved: Sequence[ResolvedLine],
) -> tuple[RuleConfirmation, ...]:
    """Owner confirmations only: neither an assistant suggestion nor an approved rule becomes a
    rule on its own. A confirmation carries the tag the owner stated in that very override."""
    found: dict[str, RuleConfirmation] = {}
    for line in resolved:
        override = line.override
        if override is None or not override.make_rule:
            continue
        if override.tag is None:
            code = "rule_without_tag" if line.tag is None else "rule_without_owner_tag"
            raise StatementValidationError.of(code, "overrides")
        confirmation = RuleConfirmation(
            merchant_key=line.line.facts.merchant_key,
            tag=override.tag,
            category=override.category,
            path=override.path,
            is_subscription=override.is_subscription,
        )
        prior = found.get(confirmation.merchant_key)
        if prior is None:
            found[confirmation.merchant_key] = confirmation
        elif prior != confirmation:
            raise StatementValidationError.of("rule_confirmation_conflict", "overrides")
    return tuple(found[key] for key in sorted(found))


@dataclass(frozen=True)
class RuleProposal:
    action: str  # insert | update | noop
    rule: MerchantRule
    previous: MerchantRule | None


def _rule_id_for(merchant_key: str) -> str:
    digest = hashlib.sha256(("merchant-rule\x1f" + merchant_key).encode("utf-8")).hexdigest()
    return "MR_" + digest[:24]


def learn_rules(
    confirmations: Sequence[RuleConfirmation], existing: Sequence[MerchantRule]
) -> tuple[RuleProposal, ...]:
    """Turn confirmations into proposed rule rows. Pure: nothing is stored or changed here."""
    if len({c.merchant_key for c in confirmations}) != len(confirmations):
        raise StatementValidationError.of("rule_confirmation_conflict", "confirmations")
    taken = {rule.rule_id for rule in existing}
    proposals: list[RuleProposal] = []
    for confirmation in confirmations:
        exact = [
            rule
            for rule in existing
            if rule.match == "exact" and rule.merchant_key == confirmation.merchant_key
        ]
        previous = max(exact, key=lambda rule: (rule.revision, rule.rule_id)) if exact else None
        if previous is None:
            rule_id = _rule_id_for(confirmation.merchant_key)
            if rule_id in taken:
                raise StatementValidationError.of("rule_id_collision", "confirmations")
            proposals.append(
                RuleProposal(
                    "insert",
                    MerchantRule(
                        rule_id=rule_id,
                        merchant_key=confirmation.merchant_key,
                        tag=confirmation.tag,
                        category=confirmation.category,
                        path=confirmation.path,
                        is_subscription=confirmation.is_subscription,
                    ),
                    None,
                )
            )
            continue
        candidate = replace(
            previous,
            tag=confirmation.tag,
            category=first_not_none(confirmation.category, previous.category),
            path=first_not_none(confirmation.path, previous.path),
            is_subscription=first_not_none(confirmation.is_subscription, previous.is_subscription),
            active=True,
        )
        if candidate == previous:
            proposals.append(RuleProposal("noop", previous, previous))
        else:
            proposals.append(
                RuleProposal("update", replace(candidate, revision=previous.revision + 1), previous)
            )
    return tuple(proposals)
