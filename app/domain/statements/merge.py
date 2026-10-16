"""Matching a statement charge to a hand-entered expense so the charge shows once.

A hand-entered expense is a candidate for a charge line only when all of these hold:
  - it is not statement-created, not cancelled and not already linked to a line;
  - its amount equals the charged ILS amount, or its currency and amount equal the line's
    original foreign currency and amount (a USD subscription entered in USD);
  - its date is within [txn_date - 3 days, charge_date + 3 days];
  - its vendor matches the merchant key or one of the matched rule's aliases.

Exactly one candidate is a proposed link. Several are a choice the owner must make, and
nothing merges on its own. Credits and cash entries never merge.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from .errors import StatementValidationError
from .expense import is_statement_source_key
from .identity import LineFacts
from .text import normalize_merchant
from .vocabulary import MIN_PREFIX_CHARS, PAYMENT_STATUS_CANCELLED

WINDOW_BEFORE_DAYS = 3
WINDOW_AFTER_DAYS = 3


@dataclass(frozen=True)
class ManualExpense:
    """A read-only view of an existing expense. The domain never modifies one."""

    expense_id: str
    source_key: str
    date: date
    vendor: str
    amount_minor: int
    currency: str
    payment_status: str
    linked_line_key: str | None = None


@dataclass(frozen=True)
class MergeItem:
    line_key: str
    facts: LineFacts
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class MatchResult:
    outcome: str  # none | one | choose
    expense_id: str | None
    candidate_ids: tuple[str, ...]


def _keys_match(left: str, right: str) -> bool:
    if left == right:
        return True
    short, long_ = (left, right) if len(left) <= len(right) else (right, left)
    return len(short) >= MIN_PREFIX_CHARS and long_.startswith(short + " ")


def vendor_matches(vendor: str, merchant_key: str, aliases: Sequence[str] = ()) -> bool:
    """Equal keys, or one is a whole-word prefix of the other (at least 3 characters)."""
    mine = normalize_merchant(vendor)
    if not mine:
        return False
    for target in (merchant_key, *aliases):
        theirs = normalize_merchant(target)
        if theirs and _keys_match(mine, theirs):
            return True
    return False


def _amount_matches(facts: LineFacts, expense: ManualExpense) -> bool:
    if expense.currency == facts.currency and expense.amount_minor == facts.amount_minor:
        return True
    return (
        facts.original_currency is not None
        and expense.currency == facts.original_currency
        and expense.amount_minor == facts.original_amount_minor
    )


def match_manual(
    facts: LineFacts,
    expenses: Sequence[ManualExpense],
    *,
    charge_date: date,
    aliases: Sequence[str] = (),
) -> MatchResult:
    """Which hand-entered expenses could be this charge. Pure: the inputs are not changed."""
    if facts.kind != "charge":
        return MatchResult("none", None, ())
    earliest = facts.txn_date - timedelta(days=WINDOW_BEFORE_DAYS)
    latest = charge_date + timedelta(days=WINDOW_AFTER_DAYS)
    found = sorted(
        expense.expense_id
        for expense in expenses
        if not is_statement_source_key(expense.source_key)
        and expense.payment_status != PAYMENT_STATUS_CANCELLED
        and expense.linked_line_key is None
        and _amount_matches(facts, expense)
        and earliest <= expense.date <= latest
        and vendor_matches(expense.vendor, facts.merchant_key, aliases)
    )
    if not found:
        return MatchResult("none", None, ())
    if len(found) == 1:
        return MatchResult("one", found[0], (found[0],))
    return MatchResult("choose", None, tuple(found))


def match_batch(
    items: Sequence[MergeItem], expenses: Sequence[ManualExpense], *, charge_date: date
) -> dict[str, MatchResult]:
    """Match every line, then withdraw any single match that two lines both claim.

    Two identical same-day charges and one hand entry would otherwise both auto-link to it.
    """
    keys = [item.line_key for item in items]
    if len(set(keys)) != len(keys):
        raise StatementValidationError.of("duplicate_line", "items")
    raw = {
        item.line_key: match_manual(
            item.facts, expenses, charge_date=charge_date, aliases=item.aliases
        )
        for item in items
    }
    claims = Counter(expense_id for result in raw.values() for expense_id in result.candidate_ids)
    settled: dict[str, MatchResult] = {}
    for key, result in raw.items():
        if result.outcome == "one" and claims[result.candidate_ids[0]] > 1:
            result = MatchResult("choose", None, result.candidate_ids)
        settled[key] = result
    return settled
