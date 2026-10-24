"""The statement import plan (V2-26; V2_PG_CONTRACTS.md §12.2, §22 E4).

Everything between "the stored rows" and "the rows to write" lives here, so the service module is
only admission, transactions and wiring. Nothing here opens a transaction or writes a table: the
loaders read through the open ``LedgerTx`` and the statement tables, ``compute`` runs the V2-25
domain (``apply_rules`` then ``diff_statement``), and the builders turn a ``StatementDiff`` into the
ledger ``Mutation`` objects and the statement rows the service writes back inside the same
``LedgerTx``.

Privacy (I7, I11): no function logs, and no exception carries statement text, a merchant or an
amount. Errors are the domain's reason codes.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Any

from app.domain.statements import (
    BLOCK_MERGE_CHOICE,
    LINKED_EXPENSE_CHANGED,
    PAYMENT_STATUS_CANCELLED,
    BatchLine,
    Classification,
    CycleTotals,
    LineFacts,
    ManualExpense,
    MerchantRule,
    OwnerOverride,
    ResolvedLine,
    ReviewChoices,
    RuleConfirmation,
    StatementDiff,
    StatementValidationError,
    StoredLine,
    apply_rules,
    confirmations_from_review,
    diff_statement,
    is_statement_source_key,
    learn_rules,
    statement_source_key,
    summarize_cycle,
)
from app.services.os_ledger import api
from app.services.os_ledger.statement_tables import (
    LineRow,
    RuleRow,
    StatementRow,
    StatementTables,
)

OWNER_EDITED_CHOICES = ("keep", "overwrite")
OVERRIDE_FIELDS = ("tag", "category", "path", "is_subscription")
_WINDOW = timedelta(days=3)  # the domain's merge window (WINDOW_BEFORE_DAYS / WINDOW_AFTER_DAYS)


# ---------------------------------------------------------------------------------------------
# Keyed ids


def rule_hmac(pepper: bytes, merchant_key: str) -> str:
    """HMAC-SHA256(pepper, "merchant-rule\\x1f" + merchant_key): the personal rule's match key."""
    message = ("merchant-rule\x1f" + merchant_key).encode("utf-8")
    return hmac.new(pepper, message, hashlib.sha256).hexdigest()


def personal_rule_id(rule_key_hmac: str) -> str:
    return "MR_" + rule_key_hmac[:24]


def retired_hmac(pepper: bytes, rule_id: str) -> str:
    """A non-matching stand-in for a retired rule's key, so its slot is free for a new rule."""
    return hmac.new(pepper, ("retired-rule\x1f" + rule_id).encode("utf-8"),
                    hashlib.sha256).hexdigest()


def expense_id_for(source_key: str) -> str:
    """Deterministic, so a plan made at submit and the commit made at approve agree."""
    return "EXP_" + hashlib.sha256(("statement-expense\x1f" + source_key).encode()).hexdigest()[:32]


def reason_error(code: str) -> api.LedgerError:
    return api.LedgerError("validation_error", details={"reason": code}, reason=code)


def domain_error(exc: StatementValidationError) -> api.LedgerError:
    """A domain refusal as a 422 ``validation_error``: the first reason code, never a value."""
    code = exc.issues[0].code if exc.issues else "invalid_statement"
    if code == "rule_without_owner_tag":  # one client-facing reason: send the tag with the tap
        code = "rule_without_tag"
    return reason_error(code)


# ---------------------------------------------------------------------------------------------
# Batch lines (the pending detail) <-> domain objects


def detail_of(line: BatchLine) -> dict[str, Any]:
    facts = line.facts
    return {
        "txn_date": facts.txn_date.isoformat(), "merchant_display": facts.merchant_display,
        "merchant_key": facts.merchant_key, "amount_minor": facts.amount_minor,
        "kind": facts.kind, "currency": facts.currency,
        "original_amount_minor": facts.original_amount_minor,
        "original_currency": facts.original_currency, "installment_n": facts.installment_n,
        "installment_of": facts.installment_of, "suggested_tag": line.suggested_tag,
        "suggested_category": line.suggested_category, "suggested_path": line.suggested_path,
        "is_subscription": line.is_subscription, "confidence": line.confidence,
    }


def batch_lines_from(rows: Iterable[Any], statement_id: str) -> list[BatchLine]:
    """Rebuild domain lines from stored detail; purged rows (no detail) are skipped."""
    lines = []
    for row in rows:
        d = row.detail
        if d is None:
            continue
        facts = LineFacts(
            txn_date=date.fromisoformat(d["txn_date"]), merchant_display=d["merchant_display"],
            merchant_key=d["merchant_key"], amount_minor=d["amount_minor"], kind=d["kind"],
            currency=d["currency"], original_amount_minor=d.get("original_amount_minor"),
            original_currency=d.get("original_currency"), installment_n=d.get("installment_n"),
            installment_of=d.get("installment_of"))
        lines.append(BatchLine(
            line_key=row.line_key, statement_id=statement_id,
            occurrence_index=row.occurrence_index, facts=facts,
            suggested_tag=d.get("suggested_tag"), suggested_category=d.get("suggested_category"),
            suggested_path=d.get("suggested_path"), is_subscription=d.get("is_subscription"),
            confidence=float(d["confidence"])))
    return sorted(lines, key=lambda x: (x.facts.merchant_key, x.facts.txn_date, x.line_key))


# ---------------------------------------------------------------------------------------------
# Rules


def _domain_rule(row: RuleRow) -> MerchantRule | None:
    if row.tag != "business" or row.merchant_key is None:
        return None
    try:
        return MerchantRule(
            rule_id=row.rule_id, merchant_key=row.merchant_key, tag="business", match=row.match,
            category=row.category, path=row.path, platform=row.platform,
            is_subscription=row.is_subscription, label=row.label, aliases=tuple(row.aliases),
            active=row.active, revision=row.revision)
    except StatementValidationError:
        return None  # a stored rule the domain refuses must not brick every batch


def business_rules(rows: Iterable[RuleRow], *, active_only: bool = True) -> list[MerchantRule]:
    found = (_domain_rule(r) for r in rows if r.active or not active_only)
    return [rule for rule in found if rule is not None]


def rules_for(lines: Sequence[BatchLine], rows: Sequence[RuleRow],
              pepper: bytes) -> list[MerchantRule]:
    """Active business rules, plus a transient exact rule for each personal rule whose HMAC matches
    one of the batch's merchant keys (the key itself is never stored for a personal rule)."""
    rules = business_rules(rows)
    by_hmac = {r.merchant_key_hmac: r for r in rows
               if r.tag == "personal" and r.active and r.match == "exact"}
    if by_hmac:
        for key in sorted({line.facts.merchant_key for line in lines}):
            row = by_hmac.get(rule_hmac(pepper, key))
            if row is not None:
                rules.append(MerchantRule(rule_id=row.rule_id, merchant_key=key, tag="personal",
                                          match="exact", revision=row.revision))
    return rules


def plan_rules(confirmations: Sequence[RuleConfirmation], rows: Sequence[RuleRow],
               pepper: bytes, proposal_id: str) -> list[tuple[RuleRow, str]]:
    """The rule rows to write for the owner's "always for this merchant" choices.

    A personal rule is keyed only by its HMAC (id and match key) and stores no merchant text. The
    business rules go through the domain's ``learn_rules`` and keep its id. A merchant that changes
    tag keeps one live rule: a personal row gains the key in place (same id). A business row that
    began as that personal row still has its id, so it goes back to personal in place too; any other
    business row is retired (key and HMAC scrubbed) before the personal row takes the HMAC slot.
    """
    by_hmac = {(r.match, r.merchant_key_hmac): r for r in rows}
    writes: list[tuple[RuleRow, str]] = []
    to_learn: list[RuleConfirmation] = []
    for c in confirmations:
        h = rule_hmac(pepper, c.merchant_key)
        row = by_hmac.get(("exact", h))
        if c.tag == "personal":
            if row is not None and row.tag == "personal":
                continue
            if row is not None and row.rule_id == personal_rule_id(h):
                # Retiring this row and inserting the personal one would insert the same id.
                writes.append((replace(
                    row, tag="personal", merchant_key=None, category=None, path=None,
                    platform=None, is_subscription=None, label=None, aliases=(), active=True,
                    revision=row.revision + 1, source_proposal_id=proposal_id), "update"))
                continue
            if row is not None:
                writes.append((replace(
                    row, active=False, revision=row.revision + 1, merchant_key="retired",
                    merchant_key_hmac=retired_hmac(pepper, row.rule_id), label=None, aliases=(),
                    source_proposal_id=proposal_id), "update"))
            writes.append((RuleRow(
                rule_id=personal_rule_id(h), tag="personal", match="exact", merchant_key=None,
                merchant_key_hmac=h, source_proposal_id=proposal_id), "insert"))
        elif row is not None and row.tag == "personal":
            writes.append((replace(
                row, tag="business", merchant_key=c.merchant_key, category=c.category,
                path=c.path, is_subscription=c.is_subscription, active=True,
                revision=row.revision + 1, source_proposal_id=proposal_id), "update"))
        else:
            to_learn.append(c)
    if to_learn:
        existing = business_rules(rows, active_only=False)
        for proposal in learn_rules(to_learn, existing):
            if proposal.action == "noop":
                continue
            rule = proposal.rule
            writes.append((RuleRow(
                rule_id=rule.rule_id, tag="business", match=rule.match,
                merchant_key=rule.merchant_key,
                merchant_key_hmac=rule_hmac(pepper, rule.merchant_key), category=rule.category,
                path=rule.path, platform=rule.platform, is_subscription=rule.is_subscription,
                label=rule.label, aliases=tuple(rule.aliases), active=rule.active,
                revision=rule.revision, source_proposal_id=proposal_id), proposal.action))
    return writes


# ---------------------------------------------------------------------------------------------
# Stored lines and hand entries


def _owner_override(value: Mapping[str, Any] | None) -> OwnerOverride | None:
    if not value:
        return None
    try:
        return OwnerOverride(**{k: v for k, v in value.items() if k in OVERRIDE_FIELDS})
    except (StatementValidationError, TypeError):
        return None


def _facts_of(row: LineRow) -> LineFacts | None:
    if row.tag != "business" or None in (row.txn_date, row.merchant_display, row.merchant_key,
                                         row.amount_minor, row.kind, row.currency):
        return None
    return LineFacts(
        txn_date=row.txn_date, merchant_display=row.merchant_display,  # type: ignore[arg-type]
        merchant_key=row.merchant_key, amount_minor=row.amount_minor,  # type: ignore[arg-type]
        kind=row.kind, currency=row.currency,  # type: ignore[arg-type]
        original_amount_minor=row.original_amount_minor, original_currency=row.original_currency,
        installment_n=row.installment_n, installment_of=row.installment_of)


def _classification_of(row: LineRow) -> Classification | None:
    if row.tag != "business" or None in (row.category, row.path, row.platform,
                                         row.is_subscription):
        return None
    return Classification(row.category, row.path,  # type: ignore[arg-type]
                          row.platform, bool(row.is_subscription))  # type: ignore[arg-type]


def view_rows(rows: Iterable[LineRow]) -> list[StoredLine]:
    """Committed rows for the cycle views: totals and subscriptions need no expense lookups."""
    return [StoredLine(
        statement_id=row.statement_id, line_key=row.line_key, tag=row.tag, state=row.state,
        facts=_facts_of(row), classification=_classification_of(row), rule_id=row.rule_id,
        revision=row.revision) for row in rows]


def load_stored(tx: Any, tables: StatementTables, statement_id: str) -> list[StoredLine]:
    """The statement's committed rows as domain ``StoredLine`` objects.

    The current revision of each expense the statement created comes from the ledger. A personal
    row cannot keep an expense id (FORK-PERSONAL-MIN), so the expense a line once created is found
    again by its deterministic source key: that is how a line that turns personal and back revives
    its cancelled expense instead of colliding with it.
    """
    rows = tables.lines(statement_id)
    refs = {api.RecordRef("expenses", i) for r in rows
            for i in (r.expense_id, r.matched_expense_id) if i}
    records = tx.get_many(sorted(refs, key=lambda r: r.record_id), include_deleted=True)
    by_id = {ref.record_id: rec for ref, rec in records.items()}
    created = {rec["source_key"]: rec
               for rec in tx.find_expenses(source_key_prefix=f"stmt:{statement_id}:")}
    stored = []
    for row in rows:
        own_key = statement_source_key(statement_id, row.line_key)
        expense = by_id.get(row.expense_id) if row.expense_id else created.get(own_key)
        imported = row.expense_revision_at_import
        if expense is not None and row.expense_id is None:
            # Re-attached (a personal or voided row): assume the statement's own void is the last
            # write, so only a restored (un-cancelled) expense reads as owner-edited.
            cancelled = expense.get("payment_status") == PAYMENT_STATUS_CANCELLED
            imported = expense["revision"] if cancelled else max(expense["revision"] - 1, 1)
        matched = by_id.get(row.matched_expense_id) if row.matched_expense_id else None
        facts = _facts_of(row)
        classification = _classification_of(row) if facts is not None else None
        stored.append(StoredLine(
            statement_id=statement_id, line_key=row.line_key, tag=row.tag, state=row.state,
            facts=facts, classification=classification, rule_id=row.rule_id,
            owner_override=_owner_override(row.owner_override),
            expense_id=expense["expense_id"] if expense is not None else None,
            expense_source_key=own_key if expense is not None else None,
            expense_revision=expense["revision"] if expense is not None else None,
            imported_expense_revision=imported if expense is not None else None,
            matched_expense_id=row.matched_expense_id if matched is not None else None,
            matched_source_key=matched["source_key"] if matched is not None else None,
            revision=row.revision))
    return stored


def load_candidates(tx: Any, tables: StatementTables, statement: StatementRow,
                    lines: Sequence[BatchLine],
                    stored: Sequence[StoredLine]) -> list[ManualExpense]:
    """Hand entries a line may link to, plus every hand entry already linked to this statement
    (§22 E4.1: leaving them out makes the stale-link check inert)."""
    linked = {row.matched_expense_id: row.line_key for row in stored
              if row.state == "active" and row.matched_expense_id}
    low = min([line.facts.txn_date for line in lines] + [statement.charge_date]) - _WINDOW
    high = max([line.facts.txn_date for line in lines] + [statement.charge_date]) + _WINDOW
    taken_elsewhere = tables.matched_elsewhere(statement.statement_id)
    found = tx.find_expenses(ids=sorted(linked), manual_window=(low, high))
    candidates = []
    for rec in found:
        if is_statement_source_key(rec.get("source_key")) or rec["expense_id"] in taken_elsewhere:
            continue
        try:
            candidates.append(ManualExpense(
                expense_id=rec["expense_id"], source_key=rec["source_key"],
                date=date.fromisoformat(rec["date"]), vendor=rec.get("vendor"),
                amount_minor=rec["amount_ils"], currency=rec["currency"],
                payment_status=rec["payment_status"],
                linked_line_key=linked.get(rec["expense_id"])))
        except (StatementValidationError, KeyError, ValueError, TypeError):
            continue  # a hand entry the domain cannot read is not a candidate
    return sorted(candidates, key=lambda c: c.expense_id)


# ---------------------------------------------------------------------------------------------
# The plan


@dataclass
class Plan:
    resolved: tuple[ResolvedLine, ...]
    diff: StatementDiff
    stored: list[StoredLine]
    candidates: list[ManualExpense]
    overrides: dict[str, dict[str, Any]]
    choices: dict[str, dict[str, Any]]  # the effective choices, stale entries dropped (E4.2)
    rule_writes: list[tuple[RuleRow, str]]
    confirmations: tuple[RuleConfirmation, ...]
    outcome: str  # a digest of every effect and every resolved value
    totals: CycleTotals | None
    resolved_by_key: dict[str, ResolvedLine] = field(default_factory=dict)
    entries_by_key: dict[str, Any] = field(default_factory=dict)


def _owner_overrides(raw: Mapping[str, Mapping[str, Any]],
                     known: set[str]) -> dict[str, OwnerOverride]:
    found: dict[str, OwnerOverride] = {}
    for key, value in raw.items():
        if key not in known:
            continue
        fields = {k: v for k, v in value.items() if k in {*OVERRIDE_FIELDS, "make_rule"}}
        found[key] = OwnerOverride(**fields)
    return found


def compute(tx: Any, tables: StatementTables, *, statement: StatementRow,
            batch: Sequence[BatchLine], rule_rows: Sequence[RuleRow], pepper: bytes,
            proposal_id: str, overrides: Mapping[str, Mapping[str, Any]],
            choices: Mapping[str, Any],
            strict: Mapping[str, Any] | None = None) -> Plan:
    """Resolve the batch and diff it against the committed statement.

    ``choices`` are the draft's choices as stored; stale ones (a candidate that was cancelled, a
    hand entry that fits again, a line that is gone) are dropped before the diff, so the batch is
    never stuck on ``merge_choice_invalid`` (§22 E4.2). ``strict`` names choices that are NEW in
    this call (a draft save): those must be valid, or the domain's reason code is raised.
    """
    lines = list(batch)
    sid = statement.statement_id
    resolved_keys = {line.line_key for line in lines}
    rules = rules_for(lines, rule_rows, pepper)
    resolved = apply_rules(lines, rules, _owner_overrides(overrides, resolved_keys))
    stored = load_stored(tx, tables, sid)
    candidates = load_candidates(tx, tables, statement, lines, stored)
    stored_keys = {row.line_key for row in stored}
    common: dict[str, Any] = dict(statement_id=sid, charge_date=statement.charge_date,
                                  manual_candidates=candidates, rules=rules)

    edited = {key: value for key, value in (choices.get("owner_edited") or {}).items()
              if key in resolved_keys | stored_keys and value in OWNER_EDITED_CHOICES}
    probe = diff_statement(stored, resolved, choices=ReviewChoices(edited, {}), **common)
    allowed = {entry.line_key: set(entry.candidate_expense_ids) for entry in probe.entries}

    if strict is not None:
        for key, value in (strict.get("owner_edited") or {}).items():
            if key not in resolved_keys | stored_keys:
                raise reason_error("choice_unknown_line")
            if value not in OWNER_EDITED_CHOICES:
                raise reason_error("choice_invalid")
        for key, value in (strict.get("merge") or {}).items():
            if key not in resolved_keys:
                raise reason_error("choice_unknown_line")
            if value is not None and (not isinstance(value, str)
                                      or value not in allowed.get(key, ())):
                raise reason_error("merge_choice_invalid")

    merge: dict[str, str | None] = {}
    claimed: set[str] = set()
    for key, value in sorted((choices.get("merge") or {}).items()):
        if key not in resolved_keys:
            continue
        if value is None:
            merge[key] = None
        elif isinstance(value, str) and value in allowed.get(key, ()):
            if value in claimed:
                if strict is not None:
                    raise reason_error("merge_choice_conflict")
                continue
            merge[key] = value
            claimed.add(value)
        elif strict is not None and key in (strict.get("merge") or {}):
            raise reason_error("merge_choice_invalid")
    try:
        diff = diff_statement(stored, resolved, choices=ReviewChoices(edited, merge), **common)
    except StatementValidationError as exc:
        if not all(i.code.startswith("merge_choice") for i in exc.issues):
            raise
        merge = {}
        diff = diff_statement(stored, resolved, choices=ReviewChoices(edited, {}), **common)

    confirmations = confirmations_from_review(resolved)
    rule_writes = plan_rules(confirmations, rule_rows, pepper, proposal_id)
    effective = {"owner_edited": dict(sorted(edited.items())), "merge": dict(sorted(merge.items()))}
    plan = Plan(
        resolved=resolved, diff=diff, stored=stored, candidates=candidates,
        overrides={k: dict(v) for k, v in sorted(overrides.items()) if k in resolved_keys},
        choices=effective, rule_writes=rule_writes, confirmations=confirmations, outcome="",
        totals=summarize_cycle(diff.next_lines),
        resolved_by_key={r.line.line_key: r for r in resolved},
        entries_by_key={e.line_key: e for e in diff.entries})
    plan.outcome = outcome_digest(plan)
    return plan


def outcome_digest(plan: Plan) -> str:
    """Digest of every effect the approval would have and every value the review shows."""
    diff = plan.diff
    return api.digest({
        "entries": [[e.line_key, e.action, e.reason, list(e.flags), e.from_tag, e.to_tag,
                     e.held_back, list(e.kept_fields), list(e.candidate_expense_ids)]
                    for e in diff.entries],
        "ops": [[o.op, o.line_key, o.expense_id, o.source_key, o.reason,
                 o.draft.as_input() if o.draft is not None else None] for o in diff.ops],
        "resolved": [[r.line.line_key, r.tag, r.category, r.path, r.platform, r.is_subscription,
                      r.rule_id, list(r.flags)] for r in plan.resolved],
        "blocking": list(diff.blocking),
        "rules": [[w.rule_id, w.tag, w.revision, how] for w, how in plan.rule_writes],
    })


def review_digest(proposal: Any, draft_revision: int, plan: Plan) -> str:
    return api.digest({
        "proposal_id": proposal.proposal_id, "base_digest": proposal.request_digest,
        "draft_revision": draft_revision, "overrides_digest": api.digest(plan.overrides),
        "choices": plan.choices, "dependency_state_digest": plan.outcome,
    })


# ---------------------------------------------------------------------------------------------
# Effects: expense mutations and statement rows


def expense_mutations(tx: Any, diff: StatementDiff, *, lenient: bool = False,
                      ) -> tuple[list[api.Mutation], dict[str, tuple[str, int]]]:
    """The ledger mutations for the diff's expense ops (link and unlink touch only line rows).

    Every before-image comes from ``tx.get`` (§22 E22). ``results`` maps a line key to the expense
    id and the revision the op leaves it at, which the line row must record (diff.py, "the
    committer contract"). ``lenient`` (submit's stored snapshot) skips an op whose expense is gone
    instead of refusing; the approval re-plans and refuses.
    """
    mutations: list[api.Mutation] = []
    results: dict[str, tuple[str, int]] = {}
    for op in diff.ops:
        if op.op in {"link", "unlink"}:
            continue
        if op.op == "create":
            assert op.draft is not None and op.source_key is not None
            eid = expense_id_for(op.source_key)
            after = {"expense_id": eid, "revision": 1, **op.draft.as_input()}
            mutation = api.Mutation("expenses", eid, "upsert", None, after)
        else:
            record = tx.get("expenses", op.expense_id, include_deleted=True)
            if record is None:
                if lenient:
                    continue
                raise api.LedgerError("target_changed", details={"reason": "expense_missing"})
            if op.op == "void":
                after = {**record, "payment_status": PAYMENT_STATUS_CANCELLED}
            else:
                assert op.draft is not None
                after = {**record, **op.draft.as_input()}
            after["revision"] = record["revision"] + 1
            mutation = api.Mutation("expenses", op.expense_id, "upsert", record, after)
        mutations.append(mutation)
        results[op.line_key] = (mutation.record_id, mutation.after["revision"])
    return mutations, results


def submit_mutations(tx: Any, plan: Plan) -> tuple[api.Mutation, ...]:
    """The proposal's stored effects at submit: the business expense effects and nothing else.

    §12.2 also lists a ``{line_key, tag: "personal"}`` marker per personal line. It is left out on
    purpose: the core ``receipt()`` resolves every stored mutation through ``tx.get(entity, ...)``,
    and a marker is not a ledger entity, so it would break the receipt read of every batch that
    has a personal line. The personal lines are recorded as key-only rows (FORK-PERSONAL-MIN).
    """
    mutations, _ = expense_mutations(tx, plan.diff, lenient=True)
    return tuple(mutations)


def line_row(stored: StoredLine, result: tuple[str, int] | None, assistant_tag: str | None,
             ) -> LineRow:
    """A ``StoredLine`` as its table row: a personal row keeps only key, tag, state and revision."""
    if stored.tag == "personal" or stored.facts is None:
        return LineRow(stored.statement_id, stored.line_key, stored.tag or "personal",
                       state=stored.state, revision=stored.revision)
    facts, cls = stored.facts, stored.classification
    expense_id, at_import = stored.expense_id, stored.imported_expense_revision
    if result is not None:
        expense_id, at_import = result[0], result[1]
    override = stored.owner_override
    fields = ({name: getattr(override, name) for name in OVERRIDE_FIELDS
               if getattr(override, name) is not None} if override else {})
    return LineRow(
        statement_id=stored.statement_id, line_key=stored.line_key, tag=stored.tag or "business",
        state=stored.state, txn_date=facts.txn_date, merchant_display=facts.merchant_display,
        merchant_key=facts.merchant_key, amount_minor=facts.amount_minor, kind=facts.kind,
        currency=facts.currency, original_amount_minor=facts.original_amount_minor,
        original_currency=facts.original_currency, installment_n=facts.installment_n,
        installment_of=facts.installment_of, category=cls.category if cls else None,
        path=cls.path if cls else None, platform=cls.platform if cls else None,
        is_subscription=cls.is_subscription if cls else None, rule_id=stored.rule_id,
        assistant_tag=assistant_tag, owner_override=fields or None, expense_id=expense_id,
        matched_expense_id=stored.matched_expense_id, expense_revision_at_import=at_import,
        revision=stored.revision)


def lines_digest(rows: Iterable[StoredLine]) -> str:
    return api.digest(sorted([r.line_key, r.tag, r.state] for r in rows))


# ---------------------------------------------------------------------------------------------
# Views


def totals_body(totals: CycleTotals | None) -> dict[str, int]:
    zero = totals is None
    return {
        "business_charge_minor": 0 if zero else totals.business_charge_minor,  # type: ignore[union-attr]
        "business_credit_minor": 0 if zero else totals.business_credit_minor,  # type: ignore[union-attr]
        "business_total_minor": 0 if zero else totals.business_total_minor,  # type: ignore[union-attr]
        "business_line_count": 0 if zero else totals.business_line_count,  # type: ignore[union-attr]
        "personal_count": 0 if zero else totals.personal_count,  # type: ignore[union-attr]
        "unassigned_count": 0 if zero else totals.unassigned_count,  # type: ignore[union-attr]
    }


def line_view(plan: Plan, line: BatchLine) -> dict[str, Any]:
    resolved = plan.resolved_by_key[line.line_key]
    entry = plan.entries_by_key[line.line_key]
    row = next((r for r in plan.diff.next_lines if r.line_key == line.line_key), None)
    cls = row.classification if row is not None else None
    facts = line.facts
    return {
        "line_key": line.line_key, "txn_date": facts.txn_date.isoformat(),
        "merchant_display": facts.merchant_display, "amount_minor": facts.amount_minor,
        "kind": facts.kind, "currency": facts.currency,
        "original_amount_minor": facts.original_amount_minor,
        "original_currency": facts.original_currency,
        "installment": ({"n": facts.installment_n, "of": facts.installment_of}
                        if facts.installment_n is not None else None),
        "suggested_tag": line.suggested_tag, "tag": entry.to_tag,
        "category": cls.category if cls else resolved.category,
        "path": cls.path if cls else resolved.path,
        "is_subscription": cls.is_subscription if cls else resolved.is_subscription,
        "rule_id": resolved.rule_id, "disagreement": bool(resolved.disagreements),
        "confidence": line.confidence, "action": entry.action, "flags": list(entry.flags),
        "candidate_expense_ids": list(entry.candidate_expense_ids),
    }


def merges_body(diff: StatementDiff) -> list[dict[str, Any]]:
    """Where the review needs the owner, or proposes a link: sorted by line key."""
    entries = {e.line_key: e for e in diff.entries}
    found: dict[str, dict[str, Any]] = {}
    for key, entry in entries.items():
        if BLOCK_MERGE_CHOICE in entry.flags:
            changed = LINKED_EXPENSE_CHANGED in entry.flags
            found[key] = {"line_key": key,
                          "expense_id": entry.candidate_expense_ids[0] if changed else None,
                          "state": LINKED_EXPENSE_CHANGED if changed else "choose",
                          "candidate_expense_ids": list(entry.candidate_expense_ids)}
    for op in diff.ops:
        if op.op in {"link", "unlink"}:
            entry = entries.get(op.line_key)
            found[op.line_key] = {
                "line_key": op.line_key, "expense_id": op.expense_id,
                "state": "proposed" if op.op == "link" else "unlinked",
                "candidate_expense_ids": list(entry.candidate_expense_ids) if entry else []}
    return [found[key] for key in sorted(found)]


def summary_of(plan: Plan) -> dict[str, int]:
    resolved = plan.resolved
    return {
        "line_count": len(resolved),
        "business_count": sum(1 for r in resolved if r.tag == "business"),
        "personal_count": sum(1 for r in resolved if r.tag == "personal"),
        "unassigned_count": sum(1 for r in resolved if r.tag is None),
        "disagreement_count": sum(1 for r in resolved if r.disagreements),
    }
