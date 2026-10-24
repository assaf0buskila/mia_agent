"""The statement batch service (V2-26; V2_PG_CONTRACTS.md §12.2, §5.3, §22 E4).

An assistant (or the owner, by paste) submits one card-statement batch. The batch becomes a pending
``statement_batch`` proposal whose full line detail lives in ``os_statement_batch_lines`` until a
decision. The owner reviews it, saves a server-side draft, then approves with the ``review_digest``
of exactly what he saw: one ``LedgerTx`` applies the expense ops, writes the statement and line
rows and the confirmed rules, purges the detail, supersedes any other pending batch of the
statement and finishes ``committed``. Reject, expiry, a revoked key and a restore finish the same
way without the expense ops. A statement write never happens outside the ledger transaction that
carries its ledger effects, and nothing here logs a line, a merchant or an amount (I7, I11).

Authorization is ONE call, ``LedgerStatementService._authorize``: V2-19's
``authorize.require(ctx, op)`` (§9). It refuses any context ``authorize`` did not mint. Routes mint
the context with V2-19's ``authorize.from_owner_*`` constructors; this service never mints one.
"""

from __future__ import annotations

import base64
import hmac
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import text

from app.domain.statements import (
    MAX_BATCH_BYTES,
    MAX_LINES,
    OwnerOverride,
    StatementValidationError,
    cycle_of,
    cycle_status,
    parse_statement_batch,
    subscription_view,
    summarize_cycle,
)
from app.domain.statements.vocabulary import MIN_PEPPER_BYTES
from app.services.os_ledger import api, authorize
from app.services.os_ledger import statement_plan as sp
from app.services.os_ledger.statement_tables import (
    BatchLineRow,
    DraftRow,
    StatementRow,
    StatementTables,
    tables_for,
)
from app.services.os_ledger.statement_wire import BatchSubmitted, binding, iso

PASTE = "ASSISTANT_PASTE"
CLAUDE = "ASSISTANT_CLAUDE"
GPT = "ASSISTANT_GPT"
SOURCE_CHANNEL = {PASTE: "OS", CLAUDE: "ASSISTANT_CLAUDE", GPT: "ASSISTANT_GPT"}
BATCH_TTL = timedelta(days=7)
CYCLE_LIST_MAX = 36
PAGE_MAX = 100
DRAFT_OVERRIDES_MAX = 100
FINGERPRINT_MESSAGE = b"assaf-os-statement-pepper-fingerprint.v1"

_LOG = logging.getLogger("mia.os_statements")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_PROPOSAL_ID = re.compile(r"PRO_[0-9a-f]{32}")
_STATEMENT_ID = re.compile(r"STM_[0-9a-f]{32}")
_OVERRIDE_KEYS = frozenset({"line_key", "tag", "category", "path", "is_subscription", "make_rule"})
_CHOICE_KEYS = frozenset({"owner_edited", "merge"})
_ZERO_COUNTS = {"add": 0, "keep": 0, "update": 0, "void": 0, "owner_edited_kept": 0,
                "kept_earlier_choice": 0}


def pepper_fingerprint(pepper: bytes) -> str:
    """The value ``AuthorityAdmin.pin_statement_pepper`` pins (§12.2)."""
    return hmac.new(pepper, FINGERPRINT_MESSAGE, "sha256").hexdigest()


def _log(op: str, outcome: str, reason: str, **counts: int) -> None:
    extra = "".join(f" {name}={int(value)}" for name, value in counts.items())
    _LOG.info("os_statements op=%s outcome=%s reason=%s%s", op, outcome, reason, extra)


def _refuse(op: str, reason: str) -> api.LedgerError:
    _LOG.warning("os_statements op=%s outcome=refused reason=%s", op, reason)
    return api.LedgerError("ledger_unavailable", reason=reason)


def _pg_pin_reader(tx: Any) -> str | None:
    """The pinned fingerprint: one column of the workspace row the ledger transaction already
    holds locked. (``LedgerTx`` has no accessor for it.)"""
    if tx.connection is None:
        raise _refuse("pin", "pin_unreadable")
    value = tx.connection.execute(
        text("SELECT statement_pepper_fp FROM assaf_os.os_workspaces WHERE workspace_id = :ws"),
        {"ws": tx.ctx.workspace}).scalar_one_or_none()
    return value.strip() if isinstance(value, str) else None


def db_revoke_watermark(owner_id: str) -> datetime | None:
    """The owner's revoke-all watermark (``revoked_before``) from the main database; fail closed."""
    try:
        from app.db.session import get_session_factory

        with get_session_factory()() as db:
            row = db.execute(
                text("SELECT revoked_before FROM owner_os_revocations WHERE owner_id = :owner"),
                {"owner": owner_id}).first()
    except Exception:
        raise _refuse("approve", "revocation_unreadable") from None
    if row is None or row[0] is None:
        return None
    return row[0] if row[0].tzinfo is not None else row[0].replace(tzinfo=UTC)


class LedgerStatementService:
    """``StatementBatchService`` (§12.2) on a ``Ledger``; every method is one ledger transaction."""

    def __init__(self, ledger: Any, settings: Any, *,
                 pin_reader: Callable[[Any], str | None] | None = None,
                 revoke_watermark: Callable[[str], datetime | None] | None = None) -> None:
        self._ledger = ledger
        self._settings = settings
        self._pin_reader = pin_reader or _pg_pin_reader
        self._revoke_watermark = revoke_watermark or db_revoke_watermark

    # ---- admission -------------------------------------------------------------------------

    def _available(self) -> None:
        settings = self._settings
        if settings.assaf_os_statements_enabled is not True or settings.assaf_os_backend != (
                "postgres"):
            raise api.LedgerError("operation_unavailable", reason="statements_off")

    def _owner(self, ctx: api.TrustedOwnerContext) -> None:
        """I6: the numeric id, in the allow-list now. A username never grants."""
        if str(ctx.numeric_owner) not in self._settings.telegram_owner_user_id_set():
            raise api.LedgerError("owner_auth_required", reason="owner_not_allowed")

    def _authorize(self, ctx: api.TrustedOwnerContext, op: str) -> None:
        """V2-19's ``require(ctx, op)``: the §9 channel x operation matrix, on a minted context."""
        authorize.require(ctx, op)

    def _admit(self, ctx: api.TrustedOwnerContext, op: str, *, owner_only: bool = False) -> None:
        self._available()
        self._owner(ctx)
        self._authorize(ctx, op)
        if owner_only and ctx.channel != "OS":
            raise api.LedgerError("channel_operation_denied", reason="owner_session_only")

    def _pepper(self, op: str) -> bytes:
        raw = self._settings.os_statement_pepper
        pepper = raw.encode("utf-8") if isinstance(raw, str) else b""
        if not pepper:
            raise _refuse(op, "statement_pepper_unset")
        if len(pepper) < MIN_PEPPER_BYTES:
            raise _refuse(op, "statement_pepper_invalid")
        return pepper

    def _check_pin(self, tx: Any, pepper: bytes, op: str) -> None:
        pinned = self._pin_reader(tx)
        if pinned is None:
            raise _refuse(op, "statement_pepper_unpinned")
        if not hmac.compare_digest(pinned, pepper_fingerprint(pepper)):
            raise _refuse(op, "statement_pepper_mismatch")

    def _tx(self, ctx: api.TrustedOwnerContext, *, purpose: str = "write",
            ) -> AbstractContextManager[Any]:
        return self._ledger.transaction(ctx, domain="statements", kind="statement_batch",
                                        purpose=purpose)

    @staticmethod
    def _batch(tx: Any, proposal_id: Any) -> api.ProposalRow:
        if type(proposal_id) is not str or _PROPOSAL_ID.fullmatch(proposal_id) is None:
            raise api.LedgerError("batch_not_found", reason="batch_id_invalid")
        proposal = tx.proposal(proposal_id)
        if proposal is None or proposal.kind != "statement_batch":
            raise api.LedgerError("batch_not_found", reason="batch_unknown")
        return proposal

    # ---- registry and pointers -------------------------------------------------------------

    @staticmethod
    def _pending_others(tx: Any, tables: StatementTables, statement_id: str, *,
                        exclude: str | None, epoch: int, heal: bool,
                        ) -> list[tuple[api.ProposalRow, DraftRow]]:
        """The other pending, unexpired batches of this statement (current epoch), oldest first."""
        found = []
        for draft in tables.open_drafts(statement_id=statement_id):
            if draft.proposal_id == exclude:
                continue
            proposal = tx.proposal(draft.proposal_id)
            if proposal is None or proposal.kind != "statement_batch" or (
                    proposal.status != "pending"):
                if heal:
                    tables.put_draft(replace(draft, open=False))
                continue
            if proposal.epoch != epoch or proposal.expires_at <= tx.now:
                continue
            found.append((proposal, draft))
        return sorted(found, key=lambda pair: (pair[0].created_at, pair[0].proposal_id))

    @staticmethod
    def _close(tables: StatementTables, proposal_id: str) -> None:
        """A decided batch: its detail is purged and it leaves the registry."""
        tables.purge_batch_lines(proposal_id)
        draft = tables.draft(proposal_id)
        if draft is not None and draft.open:
            tables.put_draft(replace(draft, open=False))

    @staticmethod
    def _refresh_pointer(tx: Any, tables: StatementTables, statement_id: str) -> None:
        statement = tables.statement(statement_id)
        if statement is None:
            return
        best: tuple[datetime, str] | None = None
        for draft in tables.open_drafts(statement_id=statement_id):
            proposal = tx.proposal(draft.proposal_id)
            if proposal is None or proposal.status != "pending":
                continue
            rank = (proposal.created_at, proposal.proposal_id)
            if best is None or rank > best:
                best = rank
        current = None if best is None else best[1]
        if current != statement.current_proposal_id:
            tables.update_statement(replace(statement, current_proposal_id=current))

    def _decided(self, tx: Any, tables: StatementTables, proposal: api.ProposalRow) -> None:
        self._close(tables, proposal.proposal_id)
        self._refresh_pointer(tx, tables, proposal.record_id)

    # ---- submit ----------------------------------------------------------------------------

    def submit(self, ctx: api.TrustedOwnerContext, raw: bytes, *, source: str) -> BatchSubmitted:
        op = "submit"
        self._available()
        self._owner(ctx)
        self._authorize(ctx, "propose_statement_batch")
        if source not in SOURCE_CHANNEL or ctx.channel != SOURCE_CHANNEL[source]:
            raise api.LedgerError("channel_operation_denied", reason="source_channel_mismatch")
        if self._settings.assaf_os_freeze:
            raise api.LedgerError("os_frozen", reason="freeze")
        pepper = self._pepper(op)
        try:
            batch = parse_statement_batch(raw, pepper=pepper, workspace=ctx.workspace)
        except StatementValidationError as exc:
            issues = [{"field": i.path, "code": i.code} for i in exc.issues]
            _log(op, "refused", "invalid_statement_batch", issues=len(issues))
            raise api.LedgerError("invalid_statement_batch", details={"issues": issues},
                                  reason=exc.issues[0].code if exc.issues else None) from None
        digest = api.digest({"batch": batch.digest, "source": source,
                             "credential_id": ctx.credential_id})
        request_id = (api.paste_request_uuid(str(ctx.numeric_owner), batch.client_batch_id)
                      if source == PASTE
                      else api.assistant_request_uuid(ctx.credential_id or "",
                                                      batch.client_batch_id))
        with self._tx(ctx) as tx:
            tables = tables_for(tx)
            self._check_pin(tx, pepper, op)
            existing = tx.proposal_by_request(request_id)
            if existing is not None:
                return self._replayed(ctx, tx, tables, existing, digest, pepper)
            statement = tables.statement(batch.statement_id)
            header = statement or StatementRow(
                batch.statement_id, batch.account_label, batch.charge_date, batch.cycle,
                batch.cycle_from, batch.cycle_to)
            try:
                plan = sp.compute(
                    tx, tables, statement=header, batch=list(batch.lines),
                    rule_rows=tables.rules(), pepper=pepper, proposal_id="", overrides={},
                    choices={})
            except StatementValidationError as exc:
                raise sp.domain_error(exc) from None
            finishes, superseded, conflict = 0, None, False
            for other, draft in self._pending_others(
                    tx, tables, batch.statement_id, exclude=None, epoch=ctx.authority_epoch,
                    heal=True):
                if draft.draft_revision == 0 and other.credential_id == ctx.credential_id:
                    tx.finish(other, status="superseded", receipt_request_id=other.rejection_id)
                    finishes += 1
                    self._close(tables, other.proposal_id)
                    superseded = other.proposal_id
                else:
                    conflict = True  # a batch with a saved draft is never discarded unasked
            dependency = api.digest({
                "statement": batch.statement_id,
                "lines": statement.lines_digest if statement is not None else None,
                "candidates": [[c.expense_id, c.date.isoformat(), c.amount_minor, c.payment_status]
                               for c in plan.candidates]})
            row = tx.insert_proposal(api.ProposalDraft(
                kind="statement_batch", source=source, request_id=request_id,  # type: ignore[arg-type]
                request_digest=digest, entity="statements", record_id=batch.statement_id,
                expected_revision=0, mutations=sp.submit_mutations(tx, plan),
                dependency_digest=dependency, expires_at=tx.now + BATCH_TTL,
                credential_id=ctx.credential_id))
            tables.insert_batch_lines([
                BatchLineRow(row.proposal_id, line.line_key, line.occurrence_index,
                             sp.detail_of(line)) for line in batch.lines])
            tables.put_draft(DraftRow(row.proposal_id, 0, {}, {}, batch.statement_id,
                                      ctx.credential_id, source, True))
            if statement is None:
                tables.insert_statement(replace(header, current_proposal_id=row.proposal_id))
            else:
                tables.update_statement(replace(statement, current_proposal_id=row.proposal_id))
            summary = sp.summary_of(plan)
            _log(op, "ok", "pending", lines=summary["line_count"], superseded=int(
                superseded is not None), conflict=int(conflict))
            return BatchSubmitted(row.proposal_id, batch.statement_id, "pending", conflict,
                                  superseded, summary, tx.version + finishes)

    def _replayed(self, ctx: api.TrustedOwnerContext, tx: Any, tables: StatementTables,
                  existing: api.ProposalRow, digest: str, pepper: bytes) -> BatchSubmitted:
        """The same request id: the stored batch for an equal digest, a mismatch otherwise."""
        if existing.kind != "statement_batch" or existing.request_digest != digest:
            raise api.LedgerError("idempotency_mismatch", reason="request_digest_differs")
        if existing.epoch != ctx.authority_epoch:
            raise api.LedgerError("authority_changed", reason="replay_epoch")
        if existing.status != "pending" or existing.expires_at <= tx.now:
            raise api.LedgerError("already_decided", reason="replay_terminal")
        statement = tables.statement(existing.record_id)
        lines = sp.batch_lines_from(tables.batch_lines(existing.proposal_id), existing.record_id)
        if statement is None or not lines:
            raise api.LedgerError("review_stale", reason="replay_without_detail")
        try:
            plan = sp.compute(tx, tables, statement=statement, batch=lines,
                              rule_rows=tables.rules(), pepper=pepper,
                              proposal_id=existing.proposal_id, overrides={}, choices={})
        except StatementValidationError as exc:
            raise sp.domain_error(exc) from None
        others = self._pending_others(tx, tables, existing.record_id, exclude=existing.proposal_id,
                                      epoch=ctx.authority_epoch, heal=False)
        _log("submit", "ok", "replayed", lines=len(lines))
        return BatchSubmitted(existing.proposal_id, existing.record_id, "pending", bool(others),
                              None, sp.summary_of(plan), tx.version)

    # ---- reads -----------------------------------------------------------------------------

    def review(self, ctx: api.TrustedOwnerContext, proposal_id: str) -> dict[str, Any]:
        self._admit(ctx, "read_own_batches", owner_only=True)
        with self._tx(ctx) as tx:
            tables = tables_for(tx)
            proposal = self._batch(tx, proposal_id)
            statement = tables.statement(proposal.record_id)
            if statement is None:
                raise api.LedgerError("batch_not_found", reason="statement_missing")
            live = tables.live_batch_line_count(proposal.proposal_id) > 0
            # A pending batch of an older epoch can never be decided (a decision finishes it
            # expired), so the review must not offer it: no plan, no digest to act on.
            older = proposal.status == "pending" and proposal.epoch != ctx.authority_epoch
            status = proposal.status
            if status == "pending" and (older or tx.now >= proposal.expires_at or not live):
                status = "expired"
            draft = tables.draft(proposal.proposal_id) or DraftRow(
                proposal.proposal_id, 0, statement_id=proposal.record_id,
                credential_id=proposal.credential_id, source=proposal.source, open=False)
            credential = (tables.credential(proposal.credential_id)
                          if proposal.credential_id else None)
            body: dict[str, Any] = {
                **binding(ctx, tx.version),
                "proposal_id": proposal.proposal_id, "statement_id": statement.statement_id,
                "cycle": statement.cycle, "account_label": statement.account_label,
                "charge_date": statement.charge_date.isoformat(),
                "actor_source": proposal.source,
                "credential_label": credential.label if credential is not None else None,
                "submitted_at": iso(proposal.created_at), "expires_at": iso(proposal.expires_at),
                "status": status, "approval_request_id": proposal.approval_id,
                "rejection_request_id": proposal.rejection_id,
                "conflict_with": [p.proposal_id for p, _ in self._pending_others(
                    tx, tables, proposal.record_id, exclude=proposal.proposal_id,
                    epoch=ctx.authority_epoch, heal=False)]
                if proposal.status == "pending" and not older else [],
                "draft_revision": draft.draft_revision,
            }
            if proposal.status == "pending" and live and not older:
                self._check_pin(tx, self._pepper("review"), "review")
                plan = self._plan(tx, tables, proposal, statement, draft, op="review")
                body.update(
                    review_digest=sp.review_digest(proposal, draft.draft_revision, plan),
                    counts={**_ZERO_COUNTS, **plan.diff.counts},
                    merges=sp.merges_body(plan.diff), blocking=list(plan.diff.blocking),
                    totals=sp.totals_body(plan.totals), line_count=len(plan.resolved))
            else:
                body.update(
                    review_digest=api.digest({"proposal_id": proposal.proposal_id,
                                              "status": status}),
                    counts=dict(_ZERO_COUNTS), merges=[], blocking=[],
                    totals=sp.totals_body(None),
                    line_count=tables.batch_line_count(proposal.proposal_id))
            return body

    def lines(self, ctx: api.TrustedOwnerContext, proposal_id: str, *, after: str | None,
              limit: int) -> dict[str, Any]:
        self._admit(ctx, "read_own_batches", owner_only=True)
        if type(limit) is not int or not 1 <= limit <= PAGE_MAX:
            raise api.LedgerError("invalid_query", reason="limit")
        pepper = self._pepper("lines")
        offset = self._decode_cursor(after, ctx.workspace, ctx.authority_epoch, proposal_id,
                                     pepper)
        with self._tx(ctx) as tx:
            tables = tables_for(tx)
            proposal = self._batch(tx, proposal_id)
            page: list[dict[str, Any]] = []
            next_cursor = None
            if proposal.status == "pending" and tables.live_batch_line_count(
                    proposal.proposal_id) > 0:
                statement = tables.statement(proposal.record_id)
                if statement is None:
                    raise api.LedgerError("batch_not_found", reason="statement_missing")
                draft = tables.draft(proposal.proposal_id) or DraftRow(
                    proposal.proposal_id, 0, statement_id=proposal.record_id)
                self._check_pin(tx, pepper, "lines")
                plan = self._plan(tx, tables, proposal, statement, draft, op="lines")
                batch = sp.batch_lines_from(tables.batch_lines(proposal.proposal_id),
                                            proposal.record_id)
                if offset > len(batch):
                    raise api.LedgerError("invalid_cursor", reason="cursor_range")
                window = batch[offset:offset + limit]
                page = [sp.line_view(plan, line) for line in window]
                if offset + limit < len(batch):
                    next_cursor = self._encode_cursor(ctx.workspace, ctx.authority_epoch,
                                                      proposal_id, offset + limit, pepper)
            elif offset:
                raise api.LedgerError("invalid_cursor", reason="cursor_range")
            return {**binding(ctx, tx.version), "lines": page, "next_cursor": next_cursor}

    @staticmethod
    def _cursor_mac(workspace: str, epoch: int, proposal_id: str, offset: int,
                    pepper: bytes) -> str:
        message = f"lines-cursor\x1f{workspace}\x1f{epoch}\x1f{proposal_id}\x1f{offset}".encode()
        return hmac.new(pepper, message, "sha256").hexdigest()

    def _encode_cursor(self, workspace: str, epoch: int, proposal_id: str, offset: int,
                       pepper: bytes) -> str:
        payload = base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")
        return f"c1.{self._cursor_mac(workspace, epoch, proposal_id, offset, pepper)}.{payload}"

    def _decode_cursor(self, token: str | None, workspace: str, epoch: int, proposal_id: str,
                       pepper: bytes) -> int:
        """An offset into the batch's fixed order. The cursor holds no statement text."""
        if token is None:
            return 0
        bad = api.LedgerError("invalid_cursor", reason="cursor_malformed")
        if type(token) is not str or len(token) > 512:
            raise bad
        parts = token.split(".")
        if len(parts) != 3 or parts[0] != "c1":
            raise bad
        try:
            offset = int(base64.urlsafe_b64decode(parts[2] + "=" * (-len(parts[2]) % 4)))
        except (ValueError, UnicodeError):
            raise bad from None
        if offset < 0 or not hmac.compare_digest(
                parts[1], self._cursor_mac(workspace, epoch, str(proposal_id), offset, pepper)):
            raise bad
        return offset

    def _plan(self, tx: Any, tables: StatementTables, proposal: api.ProposalRow,
              statement: StatementRow, draft: DraftRow, *, op: str,
              strict: Mapping[str, Any] | None = None,
              overrides: Mapping[str, Any] | None = None,
              choices: Mapping[str, Any] | None = None) -> sp.Plan:
        batch = sp.batch_lines_from(tables.batch_lines(proposal.proposal_id), proposal.record_id)
        try:
            return sp.compute(
                tx, tables, statement=statement, batch=batch, rule_rows=tables.rules(),
                pepper=self._pepper(op), proposal_id=proposal.proposal_id,
                overrides=draft.overrides if overrides is None else overrides,
                choices=draft.choices if choices is None else choices, strict=strict)
        except StatementValidationError as exc:
            raise sp.domain_error(exc) from None

    # ---- the draft -------------------------------------------------------------------------

    @staticmethod
    def _delta(overrides: Any) -> list[tuple[str, dict[str, Any]]]:
        if not isinstance(overrides, list):
            raise sp.reason_error("invalid_override")
        if len(overrides) > DRAFT_OVERRIDES_MAX:
            raise sp.reason_error("too_many_overrides")
        entries = []
        for entry in overrides:
            if not isinstance(entry, dict) or "line_key" not in entry or not set(entry) <= (
                    _OVERRIDE_KEYS):
                raise sp.reason_error("invalid_override")
            key = entry["line_key"]
            if type(key) is not str or _HEX64.fullmatch(key) is None:
                raise sp.reason_error("invalid_line_key")
            fields = {k: v for k, v in entry.items() if k != "line_key" and v is not None
                      and not (k == "make_rule" and v is False)}
            try:
                OwnerOverride(**fields)
            except (StatementValidationError, TypeError):
                raise sp.reason_error("invalid_override") from None
            entries.append((key, fields))
        return entries

    @staticmethod
    def _choice_delta(choices: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(choices, dict) or not set(choices) <= _CHOICE_KEYS or not all(
                isinstance(v, dict) for v in choices.values()):
            raise sp.reason_error("invalid_choices")
        return {name: dict(value) for name, value in choices.items()}

    def save_draft(self, ctx: api.TrustedOwnerContext, proposal_id: str, draft_revision: int,
                   overrides: Sequence[Mapping[str, Any]], choices: Mapping[str, Any],
                   ) -> dict[str, Any]:
        op = "draft"
        self._admit(ctx, "draft_statement_batch", owner_only=True)
        if type(draft_revision) is not int or draft_revision < 0:
            raise sp.reason_error("invalid_draft_revision")
        entries = self._delta(list(overrides) if isinstance(overrides, tuple) else overrides)
        new_choices = self._choice_delta(choices)
        pepper = self._pepper(op)
        with self._tx(ctx) as tx:
            tables = tables_for(tx)
            self._check_pin(tx, pepper, op)
            proposal = self._batch(tx, proposal_id)
            if proposal.status != "pending":
                raise api.LedgerError("already_decided", reason="batch_decided")
            statement = tables.statement(proposal.record_id)
            if (statement is None or tx.now >= proposal.expires_at
                    or proposal.epoch != ctx.authority_epoch
                    or tables.live_batch_line_count(proposal.proposal_id) == 0):
                raise api.LedgerError("review_stale", reason="batch_unavailable")
            draft = tables.draft(proposal.proposal_id) or DraftRow(
                proposal.proposal_id, 0, {}, {}, proposal.record_id, proposal.credential_id,
                proposal.source, True)
            if draft_revision != draft.draft_revision:
                raise api.LedgerError("draft_conflict", reason="draft_revision")
            known = {row.line_key for row in tables.batch_lines(proposal.proposal_id)}
            merged = {key: dict(value) for key, value in draft.overrides.items()}
            for key, fields in entries:
                if key not in known:
                    raise sp.reason_error("unknown_line")
                if fields:
                    merged[key] = fields
                else:
                    merged.pop(key, None)
            stored_choices = {name: dict(draft.choices.get(name) or {}) for name in _CHOICE_KEYS}
            for name, delta in new_choices.items():
                stored_choices[name].update(delta)
            plan = self._plan(tx, tables, proposal, statement, draft, op=op,
                              strict=new_choices, overrides=merged, choices=stored_choices)
            revision = draft.draft_revision + 1
            tables.put_draft(replace(draft, draft_revision=revision, overrides=merged,
                                     choices=stored_choices))
            _log(op, "ok", "saved", overrides=len(merged), blocking=len(plan.diff.blocking))
            return {**binding(ctx, tx.version), "draft_revision": revision,
                    "review_digest": sp.review_digest(proposal, revision, plan),
                    "blocking": list(plan.diff.blocking)}

    # ---- decisions -------------------------------------------------------------------------

    def approve(self, ctx: api.TrustedOwnerContext, proposal_id: str, decision_id: str,
                review_digest: str) -> api.ReceiptResult:
        return self._decide(ctx, proposal_id, decision_id, "approve", review_digest)

    def reject(self, ctx: api.TrustedOwnerContext, proposal_id: str,
               decision_id: str) -> api.ReceiptResult:
        return self._decide(ctx, proposal_id, decision_id, "reject", None)

    def _decide(self, ctx: api.TrustedOwnerContext, proposal_id: str, decision_id: str,
                decision: str, supplied_digest: str | None) -> api.ReceiptResult:
        op = decision
        self._admit(ctx, "decide_statement_batch", owner_only=True)
        api.canonical_uuid(decision_id)
        if decision == "approve" and (type(supplied_digest) is not str
                                      or _HEX64.fullmatch(supplied_digest) is None):
            raise sp.reason_error("invalid_review_digest")
        pepper = self._pepper(op)
        with self._tx(ctx) as tx:
            tables = tables_for(tx)
            self._check_pin(tx, pepper, op)
            proposal = self._batch(tx, proposal_id)
            expected = proposal.approval_id if decision == "approve" else proposal.rejection_id
            if decision_id != expected:
                raise api.LedgerError("decision_request_mismatch", reason="decision_id")
            previous = tx.receipt_for_proposal(proposal.proposal_id)
            if previous is not None:
                if previous.request_id != decision_id:
                    raise api.LedgerError("already_decided", reason="other_decision")
                return self._result(tx, previous, 0)  # a replay never writes
            if proposal.epoch != ctx.authority_epoch:
                return self._end(tx, tables, proposal, "expired", proposal.rejection_id,
                                 ("epoch_changed",))
            if tx.now >= proposal.expires_at:
                return self._end(tx, tables, proposal, "expired", decision_id, ())
            if tables.live_batch_line_count(proposal.proposal_id) == 0:
                return self._end(tx, tables, proposal, "expired", decision_id,
                                 ("restored_without_detail",))
            if decision == "reject":
                return self._end(tx, tables, proposal, "rejected", proposal.rejection_id, ())
            if self._credential_revoked(tables, proposal):
                return self._end(tx, tables, proposal, "rejected", proposal.rejection_id,
                                 ("credential_revoked",))
            return self._commit(ctx, tx, tables, proposal, decision_id, str(supplied_digest),
                                pepper)

    def _credential_revoked(self, tables: StatementTables, proposal: api.ProposalRow) -> bool:
        """A key that is revoked, missing, or no newer than the owner's revoke-all watermark."""
        if proposal.credential_id is None:
            return False
        credential = tables.credential(proposal.credential_id)
        if credential is None or credential.revoked_at is not None:
            return True
        mark = self._revoke_watermark(proposal.owner_id)
        return mark is not None and credential.created_at <= mark

    def _end(self, tx: Any, tables: StatementTables, proposal: api.ProposalRow, status: str,
             key: str, reasons: Sequence[str]) -> api.ReceiptResult:
        """A terminal decision that applies nothing: finish, purge, leave the registry."""
        receipt = tx.finish(proposal, status=status, receipt_request_id=key,  # type: ignore[arg-type]
                            reason_codes=tuple(reasons))
        self._decided(tx, tables, proposal)
        _log("decide", "ok", status, reasons=len(reasons))
        return self._result(tx, receipt, 1)

    def _commit(self, ctx: api.TrustedOwnerContext, tx: Any, tables: StatementTables,
                proposal: api.ProposalRow, decision_id: str, supplied: str, pepper: bytes,
                ) -> api.ReceiptResult:
        statement = tables.statement(proposal.record_id)
        if statement is None:
            raise api.LedgerError("ledger_integrity_failed", reason="statement_missing")
        draft = tables.draft(proposal.proposal_id) or DraftRow(
            proposal.proposal_id, 0, {}, {}, proposal.record_id, proposal.credential_id,
            proposal.source, True)
        plan = self._plan(tx, tables, proposal, statement, draft, op="approve")
        if not hmac.compare_digest(sp.review_digest(proposal, draft.draft_revision, plan),
                                   supplied):
            raise api.LedgerError("review_stale", reason="digest_differs")
        if plan.diff.blocking:
            raise api.LedgerError(plan.diff.blocking[0], reason=plan.diff.blocking[0])
        mutations, results = sp.expense_mutations(tx, plan.diff)
        try:
            if mutations:
                tx.apply(mutations, source=proposal.source, request_id=decision_id)
        except api.LedgerError as exc:
            if exc.code != "target_changed":
                raise
            return self._end(tx, tables, proposal, "target_changed", decision_id, ())
        finishes = 0
        for other, _ in self._pending_others(tx, tables, proposal.record_id,
                                             exclude=proposal.proposal_id,
                                             epoch=ctx.authority_epoch, heal=True):
            tx.finish(other, status="superseded", receipt_request_id=other.rejection_id)
            finishes += 1
            self._close(tables, other.proposal_id)
        receipt = tx.finish(proposal, status="committed", receipt_request_id=decision_id,
                            applied=mutations)
        finishes += 1
        self._write(tables, statement, proposal, plan, results, receipt.commit_version)
        self._decided(tx, tables, proposal)
        _log("approve", "ok", "committed", lines=len(plan.resolved), expenses=len(mutations),
             rules=len(plan.rule_writes))
        return self._result(tx, receipt, finishes)

    @staticmethod
    def _write(tables: StatementTables, statement: StatementRow, proposal: api.ProposalRow,
               plan: sp.Plan, results: Mapping[str, tuple[str, int]], version: int) -> None:
        """The statement, its lines and the confirmed rules, in the same transaction."""
        existing = {row.line_key: row for row in tables.lines(statement.statement_id)}
        rows = []
        for stored in plan.diff.line_writes:
            resolved = plan.resolved_by_key.get(stored.line_key)
            prior = existing.get(stored.line_key)
            tag = (resolved.line.suggested_tag if resolved is not None
                   else prior.assistant_tag if prior is not None else None)
            rows.append(sp.line_row(stored, results.get(stored.line_key), tag))
        # Lines that give up a hand entry go first, so the one-line-per-entry index always holds.
        for row in sorted(rows, key=lambda r: r.matched_expense_id is not None):
            if row.line_key in existing:
                tables.update_line(row)
            else:
                tables.insert_line(row)
        for rule, how in plan.rule_writes:
            if how == "insert":
                tables.insert_rule(rule)
            else:
                tables.update_rule(rule)
        active = sum(1 for row in plan.diff.next_lines if row.state == "active")
        tables.update_statement(replace(
            statement, status="active", line_count=active,
            lines_digest=sp.lines_digest(plan.diff.next_lines),
            approved_proposal_id=proposal.proposal_id, imported_version=version))

    @staticmethod
    def _result(tx: Any, receipt: api.ReceiptRow, finishes: int) -> api.ReceiptResult:
        records = []
        if receipt.status == "committed":
            for ref in receipt.records:
                record = tx.get(ref["entity"], ref["record_id"], include_deleted=True)
                if record is not None:
                    records.append((ref["entity"], record))
        return api.ReceiptResult(
            True, receipt, tx.proposal(receipt.proposal_id), tuple(records),
            tx.version + finishes, api.UndoView("none", None, None))

    # ---- lifecycle -------------------------------------------------------------------------

    def revoke_pending_for_credential(self, tx: Any, credential_id: str) -> int:
        """Reject every pending batch of one key, INSIDE the caller's open transaction (V2-37's
        revoke). It never opens a transaction of its own (§6)."""
        tables = tables_for(tx)
        count = 0
        for draft in tables.open_drafts(credential_id=credential_id):
            proposal = tx.proposal(draft.proposal_id)
            if proposal is None or proposal.kind != "statement_batch" or (
                    proposal.status != "pending"):
                continue
            tx.finish(proposal, status="rejected", receipt_request_id=proposal.rejection_id,
                      reason_codes=("credential_revoked",))
            self._decided(tx, tables, proposal)
            count += 1
        _log("revoke", "ok", "credential_revoked", batches=count)
        return count

    def expire_due(self, ctx_worker: api.TrustedOwnerContext) -> int:
        """Finish and purge every overdue pending batch, whatever the switches say (I11).

        Runs as the retention WORKER under the expiry admission: the kill switch,
        ``backend=sheets``, a fenced ``statements`` domain and a disabled ledger never stop it, and
        the statements flag is not consulted either.
        """
        op = "expire"
        self._authorize(ctx_worker, "expire_batches")
        pepper = self._pepper(op)
        finished = 0
        with self._tx(ctx_worker, purpose="expire") as tx:
            tables = tables_for(tx)
            self._check_pin(tx, pepper, op)
            # The registry is an index, not the authority: a pending batch that still holds detail
            # is found by that detail too, so no personal line outlives its window (I11) because
            # a registry row is missing. (LedgerTx cannot list proposals, and tx.connection is
            # barred from os_proposals.)
            found = {d.proposal_id for d in tables.open_drafts()} | set(
                tables.proposals_with_detail())
            for proposal_id in sorted(found):
                proposal = tx.proposal(proposal_id)
                if proposal is None or proposal.kind != "statement_batch" or (
                        proposal.status != "pending"):
                    draft = tables.draft(proposal_id)
                    if draft is not None and draft.open:
                        tables.put_draft(replace(draft, open=False))
                    continue
                if proposal.epoch != ctx_worker.authority_epoch:
                    reasons: tuple[str, ...] = ("epoch_changed",)
                elif proposal.expires_at < tx.now:
                    reasons = ()
                elif tables.live_batch_line_count(proposal.proposal_id) == 0:
                    reasons = ("restored_without_detail",)
                else:
                    continue
                tx.finish(proposal, status="expired", receipt_request_id=proposal.rejection_id,
                          reason_codes=reasons)
                self._decided(tx, tables, proposal)
                finished += 1
        _log(op, "ok", "done", batches=finished)
        return finished

    # ---- cycle views -----------------------------------------------------------------------

    def cycles(self, ctx: api.TrustedOwnerContext) -> dict[str, Any]:
        self._admit(ctx, "read", owner_only=True)
        with self._tx(ctx) as tx:
            tables = tables_for(tx)
            statements = tables.statements()
            if not statements:
                return {**binding(ctx, tx.version), "cycles": []}
            # §22 E31: a cycle may hold more than one statement (two charge dates), and the list
            # never hides one: a row each, newest charge date first, a placeholder if none.
            by_key: dict[tuple[str, str], list[StatementRow]] = {}
            for s in sorted(statements, key=lambda s: (s.charge_date, s.statement_id),
                            reverse=True):
                by_key.setdefault((s.account_label, s.cycle), []).append(s)
            labels = sorted({s.account_label for s in statements})
            imported = [s for s in statements if s.approved_proposal_id is not None]
            rows = tables.lines_of([s.statement_id for s in imported])
            committed = {label: {s.cycle for s in imported if s.account_label == label}
                         for label in labels}
            today = tx.now.date()
            cycle = max([cycle_of(today), *(s.cycle for s in statements)])
            entries: list[dict[str, Any]] = []
            while len(entries) < CYCLE_LIST_MAX:
                for label in labels:
                    for statement in by_key.get((label, cycle)) or [None]:
                        if len(entries) >= CYCLE_LIST_MAX:
                            break
                        entries.append(self._cycle_entry(
                            tx, tables, label, cycle, statement, rows, committed[label], today))
                cycle = _previous_cycle(cycle)
            return {**binding(ctx, tx.version), "cycles": entries}

    @staticmethod
    def _cycle_entry(tx: Any, tables: StatementTables, label: str, cycle: str,
                     statement: StatementRow | None, rows: Mapping[str, Any],
                     committed: set[str], today: date) -> dict[str, Any]:
        entry: dict[str, Any] = {
            "cycle": cycle, "account_label": label, "charge_date": None, "statement_id": None,
            "status": "", "business_total_minor": None, "business_line_count": None,
            "pending_proposal_id": None}
        pending = None
        if statement is not None:
            entry["charge_date"] = statement.charge_date.isoformat()
            if sp.awaits_owner(tx, tables, statement):
                pending = statement.current_proposal_id
        entry["pending_proposal_id"] = pending
        if statement is not None and statement.approved_proposal_id is not None:
            totals = summarize_cycle(sp.view_rows(rows[statement.statement_id]))
            entry.update(statement_id=statement.statement_id, status="imported",
                         business_total_minor=totals.business_total_minor if totals else None,
                         business_line_count=totals.business_line_count if totals else None)
        elif pending is not None:
            entry["status"] = "pending"
        else:
            # A statement that is not imported is not made so by another statement of its cycle.
            status = cycle_status(cycle, today=today,
                                  committed_cycles=committed if statement is None else set())
            entry["status"] = "pending" if status == "awaiting" else status
        return entry

    def statement_view(self, ctx: api.TrustedOwnerContext, statement_id: str) -> dict[str, Any]:
        self._admit(ctx, "read", owner_only=True)
        with self._tx(ctx) as tx:
            tables = tables_for(tx)
            statement = (tables.statement(statement_id)
                         if type(statement_id) is str and _STATEMENT_ID.fullmatch(statement_id)
                         else None)
            if statement is None or statement.approved_proposal_id is None:
                raise api.LedgerError("batch_not_found", reason="statement_unknown")
            earlier = [s for s in tables.statements()
                       if s.account_label == statement.account_label
                       and s.approved_proposal_id is not None
                       and (s.charge_date, s.statement_id) < (statement.charge_date,
                                                              statement.statement_id)]
            previous = earlier[-1] if earlier else None
            rows = tables.lines_of([statement.statement_id, *([previous.statement_id]
                                                              if previous else [])])
            current = sp.view_rows(rows[statement.statement_id])
            before = sp.view_rows(rows[previous.statement_id]) if previous else None
            totals = summarize_cycle(current)
            before_totals = summarize_cycle(before) if before is not None else None
            subscriptions = subscription_view(current, before, sp.business_rules(tables.rules()))
            return {
                **binding(ctx, tx.version),
                "statement_id": statement.statement_id, "cycle": statement.cycle,
                "account_label": statement.account_label,
                "charge_date": statement.charge_date.isoformat(),
                "cycle_from": statement.cycle_from.isoformat(),
                "cycle_to": statement.cycle_to.isoformat(),
                "totals": sp.totals_body(totals),
                "by_category": [{"category": name, "amount_minor": amount}
                                for name, amount in (totals.by_category if totals else ())],
                "subscriptions": [{
                    "label": s.label, "amount_minor": s.amount_minor,
                    "original_amount_minor": s.original_amount_minor,
                    "original_currency": s.original_currency, "line_count": s.line_count,
                    "previous_amount_minor": s.previous_amount_minor, "badges": list(s.badges)}
                    for s in subscriptions],
                "previous": ({"statement_id": previous.statement_id,
                              "business_total_minor": before_totals.business_total_minor
                              if before_totals else None} if previous else None),
            }

    def merchant_rules(self, ctx: api.TrustedOwnerContext) -> dict[str, Any]:
        self._admit(ctx, "read_rules", owner_only=True)
        with self._tx(ctx) as tx:
            rules = [r for r in tables_for(tx).rules() if r.active]
            return {**binding(ctx, tx.version), "rules": [{
                "rule_id": r.rule_id, "merchant_key": r.merchant_key, "match": r.match,
                "tag": r.tag, "category": r.category, "path": r.path, "platform": r.platform,
                "is_subscription": r.is_subscription, "label": r.label, "revision": r.revision}
                for r in rules]}

    # ---- the assistant-facing reads ----------------------------------------------------------

    def context_for_assistant(self, ctx: api.TrustedOwnerContext) -> dict[str, Any]:
        """Business rules and the last 12 statement keys, plus the exact format (§22 E4.4).

        A personal rule and every personal line are absent; a statement shows only its account,
        charge date, status and line count.
        """
        self._available()
        self._owner(ctx)
        self._authorize(ctx, "read_rules")
        with self._tx(ctx) as tx:
            tables = tables_for(tx)
            rules = [{
                "merchant_key": r.merchant_key, "match": r.match, "category": r.category,
                "path": r.path, "platform": r.platform, "is_subscription": r.is_subscription,
                "label": r.label}
                for r in tables.rules() if r.active and r.tag == "business"]
            recent = sorted(tables.statements(), key=lambda s: (s.charge_date, s.statement_id),
                            reverse=True)[:12]
            statements = []
            for s in recent:
                if s.approved_proposal_id is not None:
                    status, count = "imported", s.line_count
                elif sp.awaits_owner(tx, tables, s):
                    status, count = "pending", tables.batch_line_count(s.current_proposal_id)
                else:  # every batch of it was rejected, expired or superseded away
                    status, count = "not_imported", s.line_count
                statements.append({"account_label": s.account_label,
                                   "charge_date": s.charge_date.isoformat(), "status": status,
                                   "line_count": count})
            return {"rules": rules, "statements": statements, "format": {
                "schema": "statement-batch.v1", "encoding": "utf-8", "ensure_ascii": False,
                "separators": [",", ":"], "max_bytes": MAX_BATCH_BYTES, "max_lines": MAX_LINES}}

    def status_for_credential(self, ctx: api.TrustedOwnerContext,
                              proposal_id: str) -> dict[str, Any]:
        """The state of one of this key's own batches; anything else is ``batch_not_found``."""
        self._available()
        self._owner(ctx)
        self._authorize(ctx, "read_own_batches")
        with self._tx(ctx) as tx:
            proposal = self._batch(tx, proposal_id)
            if ctx.credential_id is None or proposal.credential_id != ctx.credential_id:
                raise api.LedgerError("batch_not_found", reason="batch_not_own")
            status, moment = proposal.status, proposal.created_at
            if status == "pending" and tx.now >= proposal.expires_at:
                status = "expired"
            receipt = tx.receipt_for_proposal(proposal.proposal_id)
            if receipt is not None:
                moment = receipt.completed_at
            return {"proposal_id": proposal.proposal_id, "status": status,
                    "updated_at": iso(moment)}


def _previous_cycle(cycle: str) -> str:
    year, month = int(cycle[:4]), int(cycle[5:])
    return f"{year - 1}-12" if month == 1 else f"{year}-{month - 1:02d}"


def build_statement_service(settings: Any) -> LedgerStatementService:
    """The production service on the PostgreSQL ledger (V2-16's ``build_ledger``)."""
    return LedgerStatementService(api.build_ledger(settings), settings)

