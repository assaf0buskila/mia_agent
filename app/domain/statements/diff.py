"""Replace semantics for re-importing one statement.

`diff_statement(previous, resolved, ...)` compares the stored rows of ONE statement with a freshly
classified import and returns what would change, as data. It never performs a write and never
reads a clock, a database or the network.

Safeguards, each covered by a test:
  - Only expenses whose source key is exactly 'stmt:<this statement_id>:<line_key>' are ever
    created, updated, revived or voided. Hand-entered, cash and ad-platform expenses are only
    ever linked to (or unlinked from), never modified.
  - A statement-created expense the owner edited after import (its revision moved past the
    import revision) is reported as 'owner_edited_kept' and left alone unless the owner chose
    'overwrite' for that line.
  - What the owner approved last time is his choice. For every field of an active, committed line
    (tag, category, path, platform, subscription flag) the order is: his explicit choice in this
    review, his earlier explicit override, the committed row, a merchant rule, the assistant.
    So a re-import with no new explicit choice from him never moves a committed line, however
    the (non-deterministic) assistant or a rule that arrived later now reads it. Where the
    newer reading differs, the entry says 'kept_earlier_choice' and lists the fields.
  - After a terminal decision a personal line keeps only its key, tag and state.
  - Every void or unlink appears as an entry. Nothing disappears silently.

The committer contract (what the persistence step must write back, because the next diff reads it):
  - create: the new expense id, its source key, and expense_revision = imported_expense_revision
    = the revision the expense was created at.
  - update, revive and void: expense_revision moves up by one and imported_expense_revision is set
    to the same value. Otherwise the statement's own write looks like an owner edit and a later
    revive or update is held back as 'owner_edited_kept'.
  - link and unlink: only the matched_* columns change; the hand entry is not otherwise touched.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any

from .errors import IssueCollector
from .expense import (
    Classification,
    ExpenseDraft,
    is_statement_source_key,
    map_to_expense,
    statement_source_key,
)
from .identity import LineFacts, is_line_key, is_statement_id
from .merge import ManualExpense, MatchResult, MergeItem, match_batch
from .rules import MerchantRule, OwnerOverride, ResolvedLine, first_not_none
from .vocabulary import PAYMENT_STATUS_CANCELLED, TAGS

ACTIONS = ("add", "keep", "update", "void", "owner_edited_kept", "kept_earlier_choice")
BLOCK_UNASSIGNED = "unassigned_lines"
BLOCK_MERGE_CHOICE = "merge_choice_required"

# Ops are listed in a safe application order: an expense freed by an unlink or void can be
# linked or reused later in the same list, so a "one active line per hand entry" index holds.
_OP_ORDER = ("unlink", "void", "update", "revive", "create", "link")
_DATE_BASES = ("charge_date", "txn_date")
_OWNER_EDITED_CHOICES = ("keep", "overwrite")
_OVERRIDE_FIELDS = ("tag", "category", "path", "is_subscription")
_CLASSIFIED_FIELDS = ("category", "path", "is_subscription", "platform")
_UNSET = object()


@dataclass(frozen=True)
class StoredLine:
    """One persisted statement line. Business lines keep their facts and classification;
    personal lines (with minimisation on) keep only key, tag and state."""

    statement_id: str
    line_key: str
    tag: str | None
    state: str = "active"  # active | replaced
    facts: LineFacts | None = None
    classification: Classification | None = None
    rule_id: str | None = None
    owner_override: OwnerOverride | None = None
    expense_id: str | None = None
    expense_source_key: str | None = None
    expense_revision: int | None = None
    imported_expense_revision: int | None = None
    matched_expense_id: str | None = None
    matched_source_key: str | None = None
    revision: int = 1

    @property
    def owner_edited(self) -> bool:
        """The statement-created expense changed after the statement last wrote it."""
        return (
            self.expense_id is not None
            and self.expense_revision is not None
            and self.imported_expense_revision is not None
            and self.expense_revision > self.imported_expense_revision
        )


@dataclass(frozen=True)
class ReviewChoices:
    """The owner's explicit decisions for the cases that need one. Validated by the diff."""

    owner_edited: Mapping[str, str] = field(default_factory=dict)  # line_key -> keep | overwrite
    merge: Mapping[str, str | None] = field(default_factory=dict)  # line_key -> expense_id | None


@dataclass(frozen=True)
class DiffEntry:
    line_key: str
    action: str
    reason: str | None = None
    flags: tuple[str, ...] = ()
    from_tag: str | None = None
    to_tag: str | None = None
    held_back: str | None = None  # the expense op an owner-edited expense blocked
    kept_fields: tuple[str, ...] = ()  # fields where an earlier owner choice was kept
    candidate_expense_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExpenseOp:
    op: str  # create | update | revive | void | link | unlink
    line_key: str
    expense_id: str | None
    source_key: str | None
    reason: str | None = None
    draft: ExpenseDraft | None = None


@dataclass(frozen=True)
class StatementDiff:
    statement_id: str
    entries: tuple[DiffEntry, ...]
    ops: tuple[ExpenseOp, ...]
    line_writes: tuple[StoredLine, ...]  # rows to insert or update
    next_lines: tuple[StoredLine, ...]  # the statement's rows after the change (pre-commit)
    blocking: tuple[str, ...]  # reasons the batch cannot be approved yet

    @property
    def is_noop(self) -> bool:
        return not self.ops and not self.line_writes

    @property
    def counts(self) -> dict[str, int]:
        counted = dict.fromkeys(ACTIONS, 0)
        for entry in self.entries:
            counted[entry.action] += 1
        return counted

    @property
    def merged_count(self) -> int:
        """Lines of this statement currently merged with a hand-entered expense."""
        return sum(1 for row in self.next_lines if row.state == "active" and row.matched_expense_id)


@dataclass(frozen=True)
class _Target:
    tag: str | None
    carried_tag: bool
    classification: Classification
    override: OwnerOverride | None
    kept_fields: tuple[str, ...]


@dataclass(frozen=True)
class _Plan:
    entry: DiffEntry
    ops: tuple[ExpenseOp, ...]
    row: StoredLine
    wrote: bool


@dataclass(frozen=True)
class _Context:
    statement_id: str
    charge_date: date
    date_basis: str
    minimize: bool
    owner_edited: Mapping[str, str]
    merge: Mapping[str, str | None]
    matches: Mapping[str, MatchResult]
    pool: Mapping[str, ManualExpense]
    known: Mapping[str, ManualExpense]  # every hand entry passed in, linked or not


def _merge_override(
    stored: OwnerOverride | None, new: OwnerOverride | None
) -> OwnerOverride | None:
    """This review's choices win field by field over the earlier ones. make_rule is one-shot."""
    merged = OwnerOverride(
        **{
            name: first_not_none(
                getattr(new, name) if new else None, getattr(stored, name) if stored else None
            )
            for name in _OVERRIDE_FIELDS
        }
    )
    return None if merged.is_empty else merged


def _approved(prev: StoredLine | None) -> dict[str, Any]:
    """What was committed for this line last time. Only an active row counts (a line that was
    replaced and has come back is decided afresh), and a personal row kept key-only has a tag
    and nothing else."""
    if prev is None or prev.state != "active":
        return {}
    approved: dict[str, Any] = {"tag": prev.tag}
    if prev.classification is not None:
        for name in _CLASSIFIED_FIELDS:
            approved[name] = getattr(prev.classification, name)
    return approved


def _target(resolved: ResolvedLine, prev: StoredLine | None) -> _Target:
    stored = prev.owner_override if prev is not None else None
    new = resolved.override
    approved = _approved(prev)

    def chosen_now(name: str) -> Any:
        return getattr(new, name, None)  # None when he did not choose it in this review

    def settled(name: str) -> Any:
        """His earlier explicit override, else the committed row. Never a rule or a suggestion."""
        earlier = getattr(stored, name, None)
        return earlier if earlier is not None else approved.get(name)

    settled_tag = settled("tag")
    if chosen_now("tag") is not None:
        tag, carried = chosen_now("tag"), False
    elif settled_tag is not None:
        # "no opinion" from the assistant never voids a committed decision
        tag = settled_tag
        carried = resolved.tag is None and getattr(stored, "tag", None) is None
    else:
        tag, carried = resolved.tag, False

    def layered(name: str) -> Any:
        earlier = settled(name)
        if chosen_now(name) is None and earlier is not None:
            return earlier
        return getattr(resolved, name)

    kept: list[str] = []
    if (
        settled_tag is not None
        and chosen_now("tag") is None
        and resolved.tag is not None
        and resolved.tag != settled_tag
    ):
        kept.append("tag")
    for name in _CLASSIFIED_FIELDS:
        earlier = settled(name)
        if earlier is not None and chosen_now(name) is None and getattr(resolved, name) != earlier:
            kept.append(name)

    return _Target(
        tag=tag,
        carried_tag=carried,
        classification=Classification(
            category=layered("category"),
            path=layered("path"),
            platform=layered("platform"),
            is_subscription=layered("is_subscription"),
        ),
        override=_merge_override(stored, new),
        kept_fields=tuple(kept),
    )


def _needs_match(resolved: ResolvedLine, prev: StoredLine | None, target: _Target) -> bool:
    """A business charge that has neither an expense of its own nor a link yet."""
    if target.tag != "business" or resolved.line.facts.kind != "charge":
        return False
    if prev is not None and prev.expense_id is not None:
        return False  # its own expense exists: update or revive it
    return not (prev is not None and prev.state == "active" and prev.matched_expense_id)


def _carried_expense(
    prev: StoredLine | None,
) -> tuple[str | None, str | None, int | None, int | None]:
    if prev is None:
        return None, None, None, None
    return (
        prev.expense_id,
        prev.expense_source_key,
        prev.expense_revision,
        prev.imported_expense_revision,
    )


def _row(
    ctx: _Context,
    resolved: ResolvedLine,
    prev: StoredLine | None,
    target: _Target,
    *,
    tag: str | None,
    matched: tuple[str | None, str | None] = (None, None),
) -> StoredLine:
    detail = tag != "personal" or not ctx.minimize
    override = target.override
    if not detail:
        override = OwnerOverride(tag=override.tag) if override and override.tag else None
    expense_id, expense_source, expense_revision, imported_revision = _carried_expense(prev)
    return StoredLine(
        statement_id=ctx.statement_id,
        line_key=resolved.line.line_key,
        tag=tag,
        state="active",
        facts=resolved.line.facts if detail else None,
        classification=target.classification if detail else None,
        rule_id=resolved.rule_id if detail else None,
        owner_override=override,
        expense_id=expense_id,
        expense_source_key=expense_source,
        expense_revision=expense_revision,
        imported_expense_revision=imported_revision,
        matched_expense_id=matched[0],
        matched_source_key=matched[1],
        revision=prev.revision if prev is not None else 1,
    )


def _finish(row: StoredLine, prev: StoredLine | None) -> tuple[StoredLine, bool]:
    """Keep the stored row object when nothing changed; otherwise bump its revision."""
    if prev is None:
        return row, True
    if replace(row, revision=prev.revision) == prev:
        return prev, False
    return replace(row, revision=prev.revision + 1), True


def _draft(ctx: _Context, line_key: str, facts: LineFacts, cls: Classification) -> ExpenseDraft:
    return map_to_expense(
        ctx.statement_id,
        line_key,
        facts,
        cls,
        charge_date=ctx.charge_date,
        date_basis=ctx.date_basis,
    )


def _held(ctx: _Context, prev: StoredLine) -> bool:
    """An owner-edited expense is never overwritten unless the owner explicitly says so."""
    return prev.owner_edited and ctx.owner_edited.get(prev.line_key) != "overwrite"


def _flags(resolved: ResolvedLine, target: _Target, *extra: str) -> tuple[str, ...]:
    flags = [f for f in resolved.flags if not (target.carried_tag and f == "unassigned")]
    if target.carried_tag:
        flags.append("assistant_tag_missing")
    if target.kept_fields:
        flags.append("kept_earlier_choice")
    flags.extend(extra)
    return tuple(dict.fromkeys(flags))


def _entry(
    resolved: ResolvedLine,
    prev: StoredLine | None,
    target: _Target,
    action: str,
    *,
    reason: str | None = None,
    extra_flags: Sequence[str] = (),
    held_back: str | None = None,
    candidates: Sequence[str] = (),
) -> DiffEntry:
    return DiffEntry(
        line_key=resolved.line.line_key,
        action=action,
        reason=reason,
        flags=_flags(resolved, target, *extra_flags),
        from_tag=prev.tag if prev is not None and prev.state == "active" else None,
        to_tag=target.tag,
        held_back=held_back,
        kept_fields=target.kept_fields,
        candidate_expense_ids=tuple(candidates),
    )


def _plain(
    resolved: ResolvedLine,
    prev: StoredLine,
    target: _Target,
    row: StoredLine,
    wrote: bool,
    *,
    update_reason: str | None = None,
) -> _Plan:
    if wrote:
        return _Plan(_entry(resolved, prev, target, "update", reason=update_reason), (), row, True)
    action = "kept_earlier_choice" if target.kept_fields else "keep"
    return _Plan(_entry(resolved, prev, target, action), (), row, False)


def _held_plan(
    resolved: ResolvedLine,
    prev: StoredLine,
    target: _Target,
    held_back: str,
    reason: str | None = None,
) -> _Plan:
    entry = _entry(resolved, prev, target, "owner_edited_kept", reason=reason, held_back=held_back)
    return _Plan(entry, (), prev, False)


def _materialize(
    ctx: _Context,
    resolved: ResolvedLine,
    prev: StoredLine | None,
    target: _Target,
    reason: str | None,
) -> _Plan:
    """A business charge with no expense yet: link a hand entry, create one, or ask."""
    key = resolved.line.line_key
    facts = resolved.line.facts
    found = ctx.matches.get(key, MatchResult("none", None, ()))
    choice = ctx.merge.get(key, _UNSET)
    if choice is _UNSET:
        outcome, chosen = found.outcome, found.expense_id
    elif choice is None:
        outcome, chosen = "none", None
    else:
        outcome, chosen = "one", choice
    if outcome == "choose":
        row, wrote = _finish(_row(ctx, resolved, prev, target, tag="business"), prev)
        entry = _entry(
            resolved,
            prev,
            target,
            "add",
            reason=reason,
            extra_flags=(BLOCK_MERGE_CHOICE,),
            candidates=found.candidate_ids,
        )
        return _Plan(entry, (), row, wrote)
    if outcome == "one" and chosen is not None:
        expense = ctx.pool[chosen]
        row, wrote = _finish(
            _row(ctx, resolved, prev, target, tag="business", matched=(chosen, expense.source_key)),
            prev,
        )
        op = ExpenseOp("link", key, chosen, expense.source_key, "linked_to_manual")
        entry = _entry(
            resolved,
            prev,
            target,
            "add",
            reason=reason or "linked_to_manual",
            candidates=found.candidate_ids,
        )
        return _Plan(entry, (op,), row, wrote)
    draft = _draft(ctx, key, facts, target.classification)
    row, wrote = _finish(_row(ctx, resolved, prev, target, tag="business"), prev)
    op = ExpenseOp("create", key, None, draft.source_key, None, draft)
    entry = _entry(resolved, prev, target, "add", reason=reason, candidates=found.candidate_ids)
    return _Plan(entry, (op,), row, wrote)


def _revive(
    ctx: _Context, resolved: ResolvedLine, prev: StoredLine, target: _Target, reason: str
) -> _Plan:
    """The line comes (back) as a business charge and already owns a cancelled expense."""
    if _held(ctx, prev):
        # The owner restored and edited that expense, so it is left exactly as he has it. The
        # line itself is in the statement again and must count in the cycle totals.
        row, wrote = _finish(_row(ctx, resolved, prev, target, tag="business"), prev)
        entry = _entry(
            resolved, prev, target, "owner_edited_kept", reason=reason, held_back="revive"
        )
        return _Plan(entry, (), row, wrote)
    key = resolved.line.line_key
    draft = _draft(ctx, key, resolved.line.facts, target.classification)
    op = ExpenseOp("revive", key, prev.expense_id, prev.expense_source_key, reason, draft)
    row, wrote = _finish(_row(ctx, resolved, prev, target, tag="business"), prev)
    return _Plan(_entry(resolved, prev, target, "add", reason=reason), (op,), row, wrote)


def _plan_fresh(
    ctx: _Context, resolved: ResolvedLine, prev: StoredLine | None, target: _Target
) -> _Plan:
    """A key with no active row: brand new, or back after having been replaced."""
    reason = "reappeared" if prev is not None else None
    facts = resolved.line.facts
    if target.tag == "business" and facts.kind == "charge":
        if prev is not None and prev.expense_id is not None:
            return _revive(ctx, resolved, prev, target, "reappeared")
        return _materialize(ctx, resolved, prev, target, reason)
    row, wrote = _finish(_row(ctx, resolved, prev, target, tag=target.tag), prev)
    return _Plan(_entry(resolved, prev, target, "add", reason=reason), (), row, wrote)


def _plan_same_tag(
    ctx: _Context, resolved: ResolvedLine, prev: StoredLine, target: _Target
) -> _Plan:
    key = resolved.line.line_key
    facts = resolved.line.facts
    if target.tag == "personal":
        row, wrote = _finish(_row(ctx, resolved, prev, target, tag="personal"), prev)
        stripped = ctx.minimize and prev.facts is not None
        return _plain(
            resolved, prev, target, row, wrote, update_reason="minimized" if stripped else None
        )

    linked = (prev.matched_expense_id, prev.matched_source_key)
    if facts.kind == "credit":
        row, wrote = _finish(_row(ctx, resolved, prev, target, tag="business"), prev)
        return _plain(resolved, prev, target, row, wrote)
    if prev.matched_expense_id is not None:
        # The owner unlinks it, or the hand entry it was merged into has been cancelled. Either
        # way the card charge exists in no live expense, so book it as its own. A hand entry that
        # is merely absent from the candidates proves nothing and changes nothing.
        linked_entry = ctx.known.get(prev.matched_expense_id)
        unlink_reason = None
        if ctx.merge.get(key, _UNSET) is None:
            unlink_reason = "unlinked_by_owner"
        elif linked_entry is not None and linked_entry.payment_status == PAYMENT_STATUS_CANCELLED:
            unlink_reason = "link_target_cancelled"
        if unlink_reason is not None:
            draft = _draft(ctx, key, facts, target.classification)
            ops = (
                ExpenseOp(
                    "unlink",
                    key,
                    prev.matched_expense_id,
                    prev.matched_source_key,
                    unlink_reason,
                ),
                ExpenseOp("create", key, None, draft.source_key, None, draft),
            )
            row, _ = _finish(_row(ctx, resolved, prev, target, tag="business"), prev)
            entry = _entry(resolved, prev, target, "update", reason=unlink_reason)
            return _Plan(entry, ops, row, True)
        row, wrote = _finish(
            _row(ctx, resolved, prev, target, tag="business", matched=linked), prev
        )
        return _plain(resolved, prev, target, row, wrote)
    if prev.expense_id is None:  # a charge that never got its expense: materialize it now
        return _materialize(ctx, resolved, prev, target, None)

    row, wrote = _finish(_row(ctx, resolved, prev, target, tag="business"), prev)
    old = (
        _draft(ctx, key, prev.facts, prev.classification)
        if prev.facts and prev.classification
        else None
    )
    new = _draft(ctx, key, facts, target.classification)
    if old == new:
        return _plain(resolved, prev, target, row, wrote)
    if _held(ctx, prev):
        return _held_plan(resolved, prev, target, "update")
    op = ExpenseOp("update", key, prev.expense_id, prev.expense_source_key, None, new)
    return _Plan(_entry(resolved, prev, target, "update"), (op,), row, True)


def _plan_to_personal(
    ctx: _Context, resolved: ResolvedLine, prev: StoredLine, target: _Target
) -> _Plan:
    """business -> personal: the derived expense is voided (or the link removed); only a
    key-only personal row remains."""
    key = resolved.line.line_key
    ops: tuple[ExpenseOp, ...] = ()
    if prev.matched_expense_id is not None:
        ops = (
            ExpenseOp(
                "unlink", key, prev.matched_expense_id, prev.matched_source_key, "became_personal"
            ),
        )
    elif prev.expense_id is not None:
        if _held(ctx, prev):
            return _held_plan(resolved, prev, target, "void", "became_personal")
        ops = (
            ExpenseOp("void", key, prev.expense_id, prev.expense_source_key, "statement_replaced"),
        )
    row, wrote = _finish(_row(ctx, resolved, prev, target, tag="personal"), prev)
    entry = _entry(resolved, prev, target, "void", reason="became_personal")
    return _Plan(entry, ops, row, wrote)


def _plan_to_business(
    ctx: _Context, resolved: ResolvedLine, prev: StoredLine, target: _Target
) -> _Plan:
    """personal -> business: the expense is created again (or revived if it once existed)."""
    facts = resolved.line.facts
    if facts.kind == "charge":
        if prev.expense_id is not None:
            return _revive(ctx, resolved, prev, target, "became_business")
        return _materialize(ctx, resolved, prev, target, "became_business")
    row, wrote = _finish(_row(ctx, resolved, prev, target, tag="business"), prev)
    return _Plan(_entry(resolved, prev, target, "add", reason="became_business"), (), row, wrote)


def _plan_present(
    ctx: _Context, resolved: ResolvedLine, prev: StoredLine | None, target: _Target
) -> _Plan:
    if prev is None or prev.state == "replaced":
        return _plan_fresh(ctx, resolved, prev, target)
    if prev.tag == target.tag:
        return _plan_same_tag(ctx, resolved, prev, target)
    if target.tag == "personal":
        return _plan_to_personal(ctx, resolved, prev, target)
    return _plan_to_business(ctx, resolved, prev, target)


def _plan_missing(ctx: _Context, prev: StoredLine) -> _Plan:
    """The key is gone from the new import: replace it, voiding what the statement created."""
    key = prev.line_key
    ops: tuple[ExpenseOp, ...] = ()
    if prev.tag == "business":
        if prev.matched_expense_id is not None:
            ops = (
                ExpenseOp(
                    "unlink",
                    key,
                    prev.matched_expense_id,
                    prev.matched_source_key,
                    "missing_from_import",
                ),
            )
        elif prev.expense_id is not None:
            if _held(ctx, prev):
                entry = DiffEntry(
                    key,
                    "owner_edited_kept",
                    "missing_from_import",
                    from_tag=prev.tag,
                    held_back="void",
                )
                return _Plan(entry, (), prev, False)
            ops = (
                ExpenseOp(
                    "void", key, prev.expense_id, prev.expense_source_key, "statement_replaced"
                ),
            )
    row = replace(
        prev,
        state="replaced",
        matched_expense_id=None,
        matched_source_key=None,
        revision=prev.revision + 1,
    )
    if prev.tag == "personal" and ctx.minimize:
        row = replace(row, facts=None, classification=None, rule_id=None)
    entry = DiffEntry(key, "void", "missing_from_import", from_tag=prev.tag)
    return _Plan(entry, ops, row, True)


def _validate(
    statement: str,
    charge_date: object,
    date_basis: object,
    minimize: object,
    previous: Sequence[StoredLine],
    resolved: Sequence[ResolvedLine],
    manual_candidates: Sequence[ManualExpense],
    rules: Sequence[MerchantRule],
    choices: object,
) -> ReviewChoices:
    issues = IssueCollector()
    if not is_statement_id(statement):
        issues.add("statement_id", "statement_id_invalid")
    if not isinstance(charge_date, date) or isinstance(charge_date, datetime):
        issues.add("charge_date", "invalid_date")
    if date_basis not in _DATE_BASES:
        issues.add("date_basis", "date_basis_invalid")
    if not isinstance(minimize, bool):
        issues.add("minimize_personal", "invalid_flag")

    seen_previous: set[str] = set()
    for index, row in enumerate(previous):
        where = f"previous[{index}]"
        if not isinstance(row, StoredLine):
            issues.add(where, "invalid_stored_line")
            continue
        if row.statement_id != statement:
            issues.add(where, "foreign_statement_line")
        if not is_line_key(row.line_key):
            issues.add(where, "invalid_stored_line")
        elif row.line_key in seen_previous:
            issues.add(where, "duplicate_line")
        seen_previous.add(row.line_key)
        if row.state not in ("active", "replaced") or row.tag not in TAGS:
            issues.add(where, "invalid_stored_line")
        if is_statement_id(statement) and is_line_key(row.line_key):
            own_key = statement_source_key(statement, row.line_key)
            if row.expense_id is not None and row.expense_source_key != own_key:
                issues.add(where, "foreign_expense_reference")
            if row.expense_id is None and row.expense_source_key is not None:
                issues.add(where, "foreign_expense_reference")
        if row.matched_expense_id is not None:
            if not isinstance(row.matched_source_key, str) or is_statement_source_key(
                row.matched_source_key
            ):
                issues.add(where, "invalid_link_target")
        elif row.matched_source_key is not None:
            issues.add(where, "invalid_link_target")
        if (
            row.state == "active"
            and row.tag == "business"
            and (row.facts is None or row.classification is None)
        ):
            issues.add(where, "invalid_stored_line")

    # A statement always has lines (the parser refuses an empty batch). An empty import would make
    # every committed line look missing and void the whole statement, so it is refused outright.
    if not resolved:
        issues.add("resolved", "empty_import")
    seen_resolved: set[str] = set()
    for index, line in enumerate(resolved):
        where = f"resolved[{index}]"
        if not isinstance(line, ResolvedLine):
            issues.add(where, "invalid_resolved_line")
        elif line.line.statement_id != statement:
            # keys of another statement never match, so every committed line would be voided
            issues.add(where, "foreign_batch_line")
        elif line.line.line_key in seen_resolved:
            issues.add(where, "duplicate_line")
        else:
            seen_resolved.add(line.line.line_key)

    seen_expenses: set[str] = set()
    for index, expense in enumerate(manual_candidates):
        where = f"manual_candidates[{index}]"
        if not isinstance(expense, ManualExpense):
            issues.add(where, "invalid_candidate")
        elif expense.expense_id in seen_expenses:
            issues.add(where, "duplicate_candidate")
        else:
            seen_expenses.add(expense.expense_id)

    rule_ids = [rule.rule_id for rule in rules if isinstance(rule, MerchantRule)]
    if len(rule_ids) != len(rules):
        issues.add("rules", "invalid_rules")
    elif len(set(rule_ids)) != len(rule_ids):
        issues.add("rules", "duplicate_rule_id")

    chosen = ReviewChoices() if choices is None else choices
    if not isinstance(chosen, ReviewChoices):
        issues.add("choices", "choice_invalid")
        issues.raise_if_any()
    for key, value in chosen.owner_edited.items():
        if key not in seen_resolved and key not in seen_previous:
            issues.add("choices.owner_edited", "choice_unknown_line")
        elif value not in _OWNER_EDITED_CHOICES:
            issues.add("choices.owner_edited", "choice_invalid")
    for key in chosen.merge:
        if key not in seen_resolved:
            issues.add("choices.merge", "choice_unknown_line")
    issues.raise_if_any()
    return chosen


def diff_statement(
    previous: Sequence[StoredLine],
    resolved: Sequence[ResolvedLine],
    *,
    statement_id: str,
    charge_date: date,
    manual_candidates: Sequence[ManualExpense] = (),
    rules: Sequence[MerchantRule] = (),
    choices: ReviewChoices | None = None,
    date_basis: str = "charge_date",
    minimize_personal: bool = True,
) -> StatementDiff:
    """What re-importing `resolved` over `previous` would change, for this statement only."""
    chosen = _validate(
        statement_id,
        charge_date,
        date_basis,
        minimize_personal,
        previous,
        resolved,
        manual_candidates,
        rules,
        choices,
    )
    prev_by_key = {row.line_key: row for row in previous}
    targets = {
        line.line.line_key: _target(line, prev_by_key.get(line.line.line_key)) for line in resolved
    }
    present = {line.line.line_key: line for line in resolved}

    # Hand entries linked to lines that stay linked are taken; those freed by a line that is
    # replaced, turned personal or unlinked can be linked again by a new line in this diff.
    taken: set[str] = set()  # expense ids that stay linked
    freed: set[str] = set()  # line keys whose link is removed in this diff
    for row in previous:
        if row.state != "active" or row.matched_expense_id is None:
            continue
        stays = (
            row.line_key in present
            and targets[row.line_key].tag == "business"
            and chosen.merge.get(row.line_key, _UNSET) is not None
        )
        if stays:
            taken.add(row.matched_expense_id)
        else:
            freed.add(row.line_key)
    pool = {
        expense.expense_id: replace(expense, linked_line_key=None)
        if expense.linked_line_key in freed
        else expense
        for expense in manual_candidates
        if expense.expense_id not in taken
    }

    rules_by_id = {rule.rule_id: rule for rule in rules}
    items = []
    for line in resolved:
        key = line.line.line_key
        if _needs_match(line, prev_by_key.get(key), targets[key]):
            rule = rules_by_id.get(line.rule_id) if line.rule_id else None
            aliases = (*rule.aliases, rule.merchant_key) if rule else ()
            items.append(MergeItem(key, line.line.facts, aliases))
    matches = match_batch(items, list(pool.values()), charge_date=charge_date)

    problems = IssueCollector()
    claimed: set[str] = set()
    for key, value in chosen.merge.items():
        if value is None:
            continue
        found = matches.get(key)
        if not isinstance(value, str) or found is None or value not in found.candidate_ids:
            problems.add("choices.merge", "merge_choice_invalid")
    problems.raise_if_any()
    for value in chosen.merge.values():
        if value is None:
            continue
        if value in claimed:
            problems.add("choices.merge", "merge_choice_conflict")
        claimed.add(value)
    problems.raise_if_any()

    ctx = _Context(
        statement_id=statement_id,
        charge_date=charge_date,
        date_basis=date_basis,
        minimize=minimize_personal,
        owner_edited=chosen.owner_edited,
        merge=chosen.merge,
        matches=matches,
        pool=pool,
        known={expense.expense_id: expense for expense in manual_candidates},
    )

    plans: list[_Plan] = []
    for line in resolved:
        key = line.line.line_key
        plans.append(_plan_present(ctx, line, prev_by_key.get(key), targets[key]))
    carried_rows: list[StoredLine] = []
    for row in sorted(previous, key=lambda stored: stored.line_key):
        if row.line_key in present:
            continue
        if row.state == "active":
            plans.append(_plan_missing(ctx, row))
        else:
            carried_rows.append(row)  # already replaced: nothing to do

    blocking = []
    if any(targets[line.line.line_key].tag is None for line in resolved):
        blocking.append(BLOCK_UNASSIGNED)
    if any(BLOCK_MERGE_CHOICE in plan.entry.flags for plan in plans):
        blocking.append(BLOCK_MERGE_CHOICE)

    ops = sorted((op for plan in plans for op in plan.ops), key=lambda op: _OP_ORDER.index(op.op))
    next_rows = [plan.row for plan in plans] + carried_rows
    return StatementDiff(
        statement_id=statement_id,
        entries=tuple(plan.entry for plan in plans),
        ops=tuple(ops),
        line_writes=tuple(plan.row for plan in plans if plan.wrote),
        next_lines=tuple(next_rows),
        blocking=tuple(blocking),
    )
