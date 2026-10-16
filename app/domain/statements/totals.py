"""Business and personal totals, the per-cycle business subscriptions list, import status.

Everything here is derived from the stored rows of ONE billing cycle. Personal lines contribute
a count only: with minimisation on, their amounts no longer exist, and the total is None
(unknown), never a guess. A cycle with no committed statement is None, never zero.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime

from .diff import StoredLine
from .errors import StatementValidationError
from .identity import is_cycle_label
from .rules import MerchantRule
from .vocabulary import DEFAULT_IMPORT_BY_DAY, MAX_IMPORT_BY_DAY


@dataclass(frozen=True)
class CycleTotals:
    business_charge_minor: int
    business_credit_minor: int
    business_total_minor: int  # charges minus credits
    business_line_count: int
    personal_count: int
    personal_total_minor: int | None  # None while personal amounts are not kept
    unassigned_count: int
    by_category: tuple[tuple[str, int], ...]  # credits netted, largest first


def summarize_cycle(rows: Iterable[StoredLine] | None) -> CycleTotals | None:
    """None for a cycle with no committed statement; zeros for one with no active lines."""
    if rows is None:
        return None
    charge = credit = business_lines = personal = unassigned = 0
    personal_total = 0
    personal_amounts_known = True
    categories: dict[str, int] = {}
    for row in rows:
        if row.state != "active":
            continue
        if row.tag is None:
            unassigned += 1
        elif row.tag == "personal":
            personal += 1
            if row.facts is None:
                personal_amounts_known = False
            else:
                sign = 1 if row.facts.kind == "charge" else -1
                personal_total += sign * row.facts.amount_minor
        else:
            if row.facts is None or row.classification is None:
                raise StatementValidationError.of("invalid_stored_line", "rows")
            business_lines += 1
            amount = row.facts.amount_minor
            if row.facts.kind == "charge":
                charge += amount
                signed = amount
            else:
                credit += amount
                signed = -amount
            name = row.classification.category
            categories[name] = categories.get(name, 0) + signed
    return CycleTotals(
        business_charge_minor=charge,
        business_credit_minor=credit,
        business_total_minor=charge - credit,
        business_line_count=business_lines,
        personal_count=personal,
        personal_total_minor=personal_total if personal_amounts_known else None,
        unassigned_count=unassigned,
        by_category=tuple(sorted(categories.items(), key=lambda item: (-item[1], item[0]))),
    )


@dataclass(frozen=True)
class SubscriptionRow:
    label: str
    amount_minor: int | None  # None for a subscription that was not charged this cycle
    original_amount_minor: int | None
    original_currency: str | None
    line_count: int
    previous_amount_minor: int | None
    badges: tuple[str, ...]  # new | changed | missing


@dataclass
class _Group:
    label: str
    amount: int = 0
    count: int = 0
    original_total: int = 0
    original_currencies: set[str] | None = None
    originals_complete: bool = True


def _is_business_charge(row: StoredLine) -> bool:
    return (
        row.state == "active"
        and row.tag == "business"
        and row.facts is not None
        and row.facts.kind == "charge"
    )


def _group_id(row: StoredLine) -> str:
    return f"rule:{row.rule_id}" if row.rule_id else f"key:{row.facts.merchant_key}"


def _groups(rows: Iterable[StoredLine], labels: dict[str, str]) -> dict[str, _Group]:
    groups: dict[str, _Group] = {}
    for row in rows:
        if not _is_business_charge(row) or row.classification is None:
            continue
        if not row.classification.is_subscription:
            continue
        facts = row.facts
        rule_label = labels.get(row.rule_id) if row.rule_id else None
        group = groups.setdefault(_group_id(row), _Group(rule_label or facts.merchant_display))
        if rule_label is None:
            group.label = min(group.label, facts.merchant_display)
        group.amount += facts.amount_minor
        group.count += 1
        if facts.original_amount_minor is None:
            group.originals_complete = False
        else:
            group.original_total += facts.original_amount_minor
            if group.original_currencies is None:
                group.original_currencies = set()
            group.original_currencies.add(facts.original_currency or "")
    return groups


def subscription_view(
    rows: Iterable[StoredLine],
    previous_rows: Iterable[StoredLine] | None,
    rules: Sequence[MerchantRule] = (),
) -> tuple[SubscriptionRow, ...]:
    """Business subscriptions of one cycle, with new / changed / missing badges.

    `previous_rows` None means the earlier cycle is unknown, so nothing is called new. An empty
    previous cycle means everything is. A subscription rule with no business charge this cycle
    is 'missing', whether or not the earlier cycle is known.
    """
    current_rows = list(rows)
    labels = {rule.rule_id: rule.label for rule in rules if rule.label}
    current = _groups(current_rows, labels)
    before = None if previous_rows is None else _groups(previous_rows, labels)

    view: list[SubscriptionRow] = []
    for group_id, group in current.items():
        badges: list[str] = []
        previous_amount = None
        if before is not None:
            earlier = before.get(group_id)
            if earlier is None:
                badges.append("new")
            else:
                previous_amount = earlier.amount
                if earlier.amount != group.amount:
                    badges.append("changed")
        one_currency = (
            group.originals_complete
            and group.original_currencies is not None
            and len(group.original_currencies) == 1
        )
        view.append(
            SubscriptionRow(
                label=group.label,
                amount_minor=group.amount,
                original_amount_minor=group.original_total if one_currency else None,
                original_currency=next(iter(group.original_currencies)) if one_currency else None,
                line_count=group.count,
                previous_amount_minor=previous_amount,
                badges=tuple(badges),
            )
        )

    charged_rules = {
        row.rule_id for row in current_rows if _is_business_charge(row) and row.rule_id
    }
    for rule in rules:
        if not (rule.active and rule.is_subscription is True and rule.tag == "business"):
            continue
        if rule.rule_id in charged_rules:
            continue
        earlier = None if before is None else before.get(f"rule:{rule.rule_id}")
        view.append(
            SubscriptionRow(
                label=rule.label or rule.merchant_key,
                amount_minor=None,
                original_amount_minor=None,
                original_currency=None,
                line_count=0,
                previous_amount_minor=earlier.amount if earlier else None,
                badges=("missing",),
            )
        )
    return tuple(sorted(view, key=lambda item: (item.label.casefold(), item.label)))


def cycle_status(
    cycle: str,
    *,
    committed_cycles: Collection[str],
    today: date,
    import_by_day: int = DEFAULT_IMPORT_BY_DAY,
) -> str:
    """imported | awaiting | not_imported. Assaf imports by the 20th; after that day a cycle
    with no committed statement is 'not imported' (null in the views, never a zero)."""
    if not is_cycle_label(cycle):
        raise StatementValidationError.of("invalid_cycle", "cycle")
    if not isinstance(today, date) or isinstance(today, datetime):
        raise StatementValidationError.of("invalid_date", "today")
    if type(import_by_day) is not int or not 1 <= import_by_day <= MAX_IMPORT_BY_DAY:
        raise StatementValidationError.of("invalid_import_day", "import_by_day")
    if cycle in committed_cycles:
        return "imported"
    year, month = int(cycle[:4]), int(cycle[5:])
    return "awaiting" if today <= date(year, month, import_by_day) else "not_imported"
