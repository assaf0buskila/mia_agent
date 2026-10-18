"""The PostgreSQL ledger core (V2-16): ``api.Ledger`` on the ``assaf_os`` schema.

``build_ledger(settings)`` returns a ``PgLedger`` on the runtime DSN
(``owner_os_ledger_database_url``, role ``assaf_os_ledger``). Its contract is
V2_PG_CONTRACTS.md §5 and the merged conformance suite, which runs unchanged on this module and on
the FakeLedger; the business logic of ``propose``/``decide`` mirrors the FakeLedger line by line.

Admission of ``transaction(ctx, domain=, kind=, purpose=)`` (§5.3), in order:
  1. the §5.5 guard (cached per engine);
  2. ``assaf_os_ledger_enabled``, then the kill switch;
  3. ``assaf_os_backend == "postgres"`` for every kind except ``import``;
  5. ``SELECT ... FOR UPDATE`` of ``os_workspaces`` in one round trip with the per-transaction
     guard (no row: ``ledger_unavailable``, reason ``workspace_missing``);
  6. ``ctx.authority_epoch == settings.assaf_os_authority_epoch == os_workspaces.epoch ==`` the
     domain row's epoch, and the numeric owner, else ``authority_changed``;
  7. the authority state per kind (``authority_not_active``).
The freeze is not checked here (a decide opens a transaction too): ``propose`` and the other
new-intent services check it first. ``purpose="expire"`` runs only steps 1, 5 and 6.

Exit: a normal exit commits (``synchronous_commit = on``); an exception rolls back and re-raises a
``LedgerError`` unchanged, maps SQLSTATE class 23 to ``ledger_integrity_failed`` and anything else
to ``ledger_unavailable``; a failure during COMMIT is ``unknown``.

Readers (§8; V2-18 owns their optimised PG versions in ``reads.py``) run REPEATABLE READ READ
ONLY, never write, and use HMAC-signed cursors bound to the workspace, listing, entity, epoch and
version. Logs carry reason codes only.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import secrets
from collections.abc import Callable, Iterable
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from app.services.os_ledger import api
from app.services.os_ledger.domain import ENUMS, DomainError, canonical_input
from app.services.os_ledger.guard import (
    ADMISSION_SQL,
    AUTHORITY_SQL,
    READ_ADMISSION_SQL,
    check_identity,
    dispose_engines,
    engine_for,
    guard_for,
    refuse,
)
from app.services.os_ledger.ids import (
    PREFIXES,
    default_uuid4,
    envelope_digest,
    hex_id,
    proposal_request_digest,
)
from app.services.os_ledger.summaries import FullSummaries, SummaryInputError, monthly_rows
from app.services.os_ledger.tx import (
    FullCoreChecks,
    PgLedgerTx,
    ScopedCoreChecks,
    database_error,
    held,
    integrity,
    wire,
)
from app.services.os_ledger.validate_full import FullStateValidator, domain_error

__all__ = ["PgLedger", "build_ledger", "dispose_engines"]

_LOG = logging.getLogger("mia.os_ledger")
DOMAINS = ("core", "statements")
KINDS = ("proposal", "owner_direct", "owner_undo", "statement_batch", "import")
PROPOSAL_TTL = timedelta(minutes=10)
_OPEN_TASKS = ENUMS["task_status"][:3]
_DONE_TASK = ENUMS["task_status"][3]
_CURSOR_RE = re.compile(r"c1\.[A-Za-z0-9_-]{1,400}\.[A-Za-z0-9_-]{22}")
_PROCESS_CURSOR_KEY = secrets.token_bytes(32)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class _Cursors:
    """Opaque page cursors, HMAC-signed per workspace over {listing, entity, epoch, version,
    offset}. A forged, malformed or foreign cursor is ``invalid_cursor``; one of another epoch or
    version is ``cursor_invalidated``. The key is ``owner_os_cursor_secret`` (>= 32 bytes) or a
    per-process random key."""

    def __init__(self, secret: str) -> None:
        raw = secret.encode("utf-8") if isinstance(secret, str) else b""
        self._secret = raw if len(raw) >= 32 else _PROCESS_CURSOR_KEY

    def _tag(self, workspace: str, payload: bytes) -> bytes:
        key = hmac.new(self._secret, b"os-ledger-cursor\0" + workspace.encode("utf-8"),
                       hashlib.sha256).digest()
        return hmac.new(key, payload, hashlib.sha256).digest()[:16]

    def encode(self, workspace: str, listing: str, entity: str, epoch: int, version: int,
               offset: int) -> str:
        payload = json.dumps({"l": listing, "e": entity, "p": epoch, "v": version, "o": offset},
                             separators=(",", ":"), sort_keys=True).encode("ascii")
        return f"c1.{_b64(payload)}.{_b64(self._tag(workspace, payload))}"

    def decode(self, token: Any, workspace: str, listing: str,
               entity: str) -> tuple[int, int, int]:
        if not isinstance(token, str) or not _CURSOR_RE.fullmatch(token):
            raise api.LedgerError("invalid_cursor")
        _, body, tag = token.split(".")
        try:
            payload = _unb64(body)
            given = _unb64(tag)
        except (ValueError, binascii.Error):
            raise api.LedgerError("invalid_cursor") from None
        if not hmac.compare_digest(given, self._tag(workspace, payload)):
            raise api.LedgerError("invalid_cursor")
        try:
            data = json.loads(payload)
        except ValueError:
            raise api.LedgerError("invalid_cursor") from None
        if (not isinstance(data, dict) or set(data) != {"l", "e", "p", "v", "o"}
                or (data["l"], data["e"]) != (listing, entity)
                or not all(type(data[k]) is int and data[k] >= 0 for k in ("p", "v", "o"))):
            raise api.LedgerError("invalid_cursor")
        return data["p"], data["v"], data["o"]


class PgLedger:
    """``api.Ledger`` on PostgreSQL. Settings are read at every call (never cached)."""

    def __init__(self, settings: Any, *, guard: Any, engine: Any,
                 clock: Callable[[], datetime] | None = None,
                 uuid4: Callable[[], str] = default_uuid4,
                 validator: api.StateValidator | None = None,
                 summaries: api.SummaryMaintainer | None = None,
                 checks: Any = None) -> None:
        self.settings = settings
        self.clock = clock
        self.uuid4 = uuid4
        self.validator = validator or FullStateValidator()
        self.summaries = summaries or FullSummaries()
        # The transition, dependency-digest and propose checks (§22 E23): V2-16's full ones by
        # default here; build_ledger() passes the scoped ones the property tests prove equal.
        self.checks = checks or FullCoreChecks()
        self._guard = guard
        self._engine = engine
        self._cursors = _Cursors(getattr(settings, "owner_os_cursor_secret", "") or "")

    # ---- settings (read at call time) --------------------------------------------------------

    def _setting(self, name: str, default: Any) -> Any:
        return getattr(self.settings, name, default)

    def _undo_window_s(self) -> int:
        return int(self._setting("owner_undo_window_seconds", 30))

    # ---- connections ---------------------------------------------------------------------

    def _connect(self):
        try:
            return self._engine.connect()
        except SQLAlchemyError:
            raise refuse("db_unreachable") from None

    def _commit(self, trans: Any) -> None:
        trans.commit()

    @staticmethod
    def _discard(trans: Any, conn: Any) -> None:
        try:
            if trans is not None and trans.is_active:
                trans.rollback()
        except SQLAlchemyError:
            pass
        try:
            conn.close()
        except SQLAlchemyError:
            pass

    def _binding(self, ctx: api.TrustedOwnerContext, owner_id: str, epoch: int,
                 authority: dict[str, tuple[str, int]], domain: str) -> None:
        if domain not in authority:
            raise integrity("authority_rows")
        settings_epoch = self._setting("assaf_os_authority_epoch", 0)
        if (type(ctx.numeric_owner) is not int or str(ctx.numeric_owner) != owner_id
                or type(settings_epoch) is not int
                or not (ctx.authority_epoch == settings_epoch == epoch
                        == authority[domain][1])):
            raise api.LedgerError("authority_changed")

    def _tx(self, conn: Any, ctx: api.TrustedOwnerContext, row: Any, *, kind: str,
            purpose: str) -> PgLedgerTx:
        now = self.clock() if self.clock is not None else row.db_now
        return PgLedgerTx(conn, ctx, kind=kind, purpose=purpose, now=now,
                          version=int(row.version), uuid4=self.uuid4,
                          undo_window_s=self._undo_window_s, validator=self.validator,
                          summaries=self.summaries, checks=self.checks)

    # ---- LedgerStore ---------------------------------------------------------------------

    @contextmanager
    def transaction(self, ctx: api.TrustedOwnerContext, *, domain: api.Domain,
                    kind: api.ProposalKind, purpose: str = "write"):
        if not isinstance(ctx, api.TrustedOwnerContext):
            raise api.LedgerError("invalid_trusted_context")
        if domain not in DOMAINS:
            raise integrity("transaction_domain")
        if kind not in KINDS:
            raise integrity("transaction_kind")
        if purpose not in {"write", "expire"}:
            raise integrity("transaction_purpose")
        if purpose == "expire" and (kind != "statement_batch" or ctx.channel != "WORKER"):
            raise AssertionError("ledger_invariant:expire_scope")
        self._guard.ensure()  # 1
        if purpose == "write":
            if not self._setting("assaf_os_ledger_enabled", False):  # 2
                raise refuse("ledger_disabled")
            if self._setting("kill_switch", True):
                raise api.LedgerError("kill_switch")
            if kind != "import" and self._setting("assaf_os_backend", "") != "postgres":  # 3
                raise api.LedgerError("backend_not_postgres")
        with held(ctx.workspace):
            conn = self._connect()
            trans = None
            try:
                trans = conn.begin()
                row = conn.execute(ADMISSION_SQL, {"ws": ctx.workspace}).one()  # 5
                check_identity(row.current_role, row.session_role)
                if row.owner_id is None:
                    raise refuse("workspace_missing")
                authority = {
                    r.domain: (r.state, int(r.epoch))
                    for r in conn.execute(AUTHORITY_SQL, {"ws": ctx.workspace})
                }
                self._binding(ctx, row.owner_id, int(row.epoch), authority, domain)  # 6
                if purpose == "write":  # 7
                    core = authority["core"][0]
                    required = "shadow" if kind == "import" else "active"
                    if core != required or (kind == "statement_batch"
                                            and authority["statements"][0] != "active"):
                        raise api.LedgerError("authority_not_active")
                tx = self._tx(conn, ctx, row, kind=kind, purpose=purpose)
            except BaseException as exc:
                self._discard(trans, conn)
                if isinstance(exc, SQLAlchemyError):
                    raise database_error(exc) from None
                raise
            try:
                yield tx
            except BaseException as exc:
                self._discard(trans, conn)
                if isinstance(exc, (api.LedgerError, AssertionError, KeyboardInterrupt,
                                    SystemExit, GeneratorExit)):
                    raise
                if isinstance(exc, SQLAlchemyError):
                    raise database_error(exc) from None
                _LOG.warning("os_ledger op=transaction outcome=refused reason=unexpected_error")
                raise api.LedgerError("ledger_unavailable", reason="unexpected_error") from None
            if tx.poisoned is not None:  # a swallowed database error never commits
                self._discard(trans, conn)
                raise tx.poisoned
            try:
                self._commit(trans)
            except BaseException:
                self._discard(trans, conn)
                _LOG.warning("os_ledger op=commit outcome=unknown reason=commit_failed")
                raise api.LedgerError("unknown") from None
            conn.close()

    @contextmanager
    def _read(self, ctx: api.TrustedOwnerContext):
        """A REPEATABLE READ READ ONLY snapshot; reads need core active or fenced (or shadow for
        the WORKER channel), at the context's epoch."""
        if not isinstance(ctx, api.TrustedOwnerContext):
            raise api.LedgerError("invalid_trusted_context")
        self._guard.ensure()
        if not self._setting("assaf_os_ledger_enabled", False):
            raise refuse("ledger_disabled")
        with held(ctx.workspace):
            conn = self._connect()
            trans = None
            try:
                trans = conn.begin()
                conn.exec_driver_sql(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                row = conn.execute(READ_ADMISSION_SQL, {"ws": ctx.workspace}).one()
                check_identity(row.current_role, row.session_role)
                if row.owner_id is None:
                    raise refuse("workspace_missing")
                authority = {d: (s, int(e)) for d, (s, e) in (row.authority or {}).items()}
                self._binding(ctx, row.owner_id, int(row.epoch), authority, "core")
                allowed = {"active", "fenced"} | ({"shadow"} if ctx.channel == "WORKER" else set())
                if authority["core"][0] not in allowed:
                    raise api.LedgerError("authority_not_active")
                tx = self._tx(conn, ctx, row, kind="proposal", purpose="read")
                tx.pending_seq = int(row.pending_seq)
                tx.states = {d: authority[d][0] for d in DOMAINS}
                yield tx
            except SQLAlchemyError as exc:
                raise database_error(exc) from None
            finally:
                self._discard(trans, conn)

    # ---- LedgerCore ------------------------------------------------------------------------

    def propose(self, ctx: api.TrustedOwnerContext, req: api.ProposeInput) -> api.ProposalRow:
        """A pending ``kind="proposal"`` (OS or MIA); replays return the stored proposal."""
        if self._setting("assaf_os_freeze", True):
            raise api.LedgerError("os_frozen")
        api.canonical_uuid(req.request_id)
        if ctx.channel not in {"OS", "MIA"}:
            raise api.LedgerError("channel_operation_denied")
        if type(req.expected_revision) is not int or req.expected_revision < 0:
            raise api.LedgerError("validation_error", details={"reason": "invalid_revision"})
        promote = req.entity == "promote_lead"
        try:
            if req.record_id is not None:
                canonical_input("tasks", {"person_id": req.record_id}, creating=False)
            if promote:
                if req.input or req.record_id is None or req.expected_revision < 1:
                    raise DomainError("invalid_promotion_input")
                values: dict[str, Any] = {}
            else:
                if not req.input:
                    raise DomainError("non_input_field")
                values = canonical_input(req.entity, dict(req.input),
                                         creating=req.record_id is None)
        except DomainError as exc:
            raise domain_error(exc, "validation_error") from None
        outer = req.approval_expires_at
        request_digest = proposal_request_digest(
            ctx, req.entity, req.record_id, req.expected_revision, values,
            outer.isoformat() if outer else None)
        if outer is not None and outer.utcoffset() is None:
            raise api.LedgerError("validation_error",
                                  details={"reason": "invalid_approval_expiry"})
        with self.transaction(ctx, domain="core", kind="proposal") as tx:
            existing = tx.proposal_by_request(req.request_id)
            if existing:
                if existing.request_digest != request_digest or existing.kind != "proposal":
                    raise api.LedgerError("idempotency_mismatch")
                return existing
            target_entity = "leads" if promote else req.entity
            before = tx.get(target_entity, req.record_id) if req.record_id else None
            if (req.record_id is None and req.expected_revision != 0) or (
                req.record_id is not None and (before is None or
                                              before["revision"] != req.expected_revision)
            ):
                raise api.LedgerError("target_changed")
            record_id = req.record_id or hex_id(PREFIXES[req.entity], self.uuid4)
            try:
                mutations = (self._promotion(tx.get("clients", before["person_id"]), before,
                                             record_id, req.expected_revision)
                             if promote else self._single(req.entity, before, record_id,
                                                          req.expected_revision, values))
                # Each transition, then validate_state and build_summaries over the prospective
                # state (tx.full_proposal_check), scoped by default (§22 E23).
                self.checks.proposal(tx, mutations)
            except (DomainError, SummaryInputError) as exc:
                raise domain_error(exc, "validation_error") from None
            if req.entity == "leads" and tx.lead_person_taken(
                    mutations[0].after["person_id"], record_id):
                raise api.LedgerError("lead_already_exists")
            expiry = tx.now + PROPOSAL_TTL
            if outer is not None:
                expiry = min(expiry, outer)
            if expiry <= tx.now:
                raise api.LedgerError("validation_error",
                                      details={"reason": "invalid_approval_expiry"})
            return tx.insert_proposal(api.ProposalDraft(
                "proposal", ctx.channel, req.request_id, request_digest, req.entity, record_id,
                req.expected_revision, mutations, self.checks.dependency_digest(tx, mutations),
                expiry))

    def _single(self, entity: str, before: dict | None, record_id: str, expected: int,
                values: dict[str, Any]) -> tuple[api.Mutation, ...]:
        after = (before or {}) | values | {api.ID_KEYS[entity]: record_id,
                                           "revision": expected + 1}
        if entity == "leads":
            after["person_id"] = (before["person_id"] if before else
                                  hex_id("PER", self.uuid4))
        return (api.Mutation(entity, record_id, "upsert", before, after),)

    @staticmethod
    def _promotion(existing: dict | None, before: dict, record_id: str,
                   expected: int) -> tuple[api.Mutation, ...]:
        """The lead's conversion plus its client. ``existing`` is the live client whose id is
        the lead's ``person_id`` (V2-16 looked it up in the full snapshot: the same record)."""
        client_status = ENUMS["client_status"][0]
        lead_after = before | {"status": ENUMS["lead_status"][-1], "revision": expected + 1}
        mutations = [api.Mutation("leads", record_id, "upsert", before, lead_after)]
        person = before["person_id"]
        if existing is None:
            client = canonical_input("clients", {
                "name": before["name"], "phone": before.get("phone") or None,
                "email": before.get("email") or None, "status": client_status, "notes": None})
            mutations.append(api.Mutation("clients", person, "upsert", None,
                                          client | {"person_id": person, "revision": 1}))
        else:
            mutations.append(api.Mutation("clients", person, "upsert", existing,
                                          existing | {"revision": existing["revision"] + 1}))
        return tuple(mutations)

    def decide(self, ctx: api.TrustedOwnerContext, proposal_id: str, decision_id: str,
               decision: str) -> api.ReceiptResult:
        """Approve or reject a ``kind="proposal"`` (any other kind is ``proposal_not_found``)."""
        api.canonical_uuid(decision_id)
        if ctx.channel not in {"OS", "TELEGRAM_APPROVAL"}:
            raise api.LedgerError("owner_approval_required")
        if decision not in {"approve", "reject"}:
            raise api.LedgerError("validation_error", details={"reason": "invalid_decision"})
        with self.transaction(ctx, domain="core", kind="proposal") as tx:
            proposal = tx.proposal(proposal_id)
            if proposal is None or proposal.kind != "proposal":
                raise api.LedgerError("proposal_not_found")
            if envelope_digest(proposal) != proposal.envelope_digest:
                raise integrity("envelope_mismatch")  # the stored proposal was altered
            expected = proposal.approval_id if decision == "approve" else proposal.rejection_id
            if decision_id != expected:
                raise api.LedgerError("decision_request_mismatch")
            previous = tx.receipt_for_proposal(proposal_id)
            if previous is not None:
                # The same decision replays the stored receipt; another is a second decision. A
                # replay is a read of an existing receipt, so it never crosses an epoch.
                if previous.request_id != decision_id:
                    raise api.LedgerError("already_decided")
                return self._result(tx, previous)
            status = "committed" if decision == "approve" else "rejected"
            reasons: tuple[str, ...] = ()
            if proposal.epoch != ctx.authority_epoch:
                status, decision_id, reasons = "expired", proposal.rejection_id, ("epoch_changed",)
            elif tx.now >= proposal.expires_at:
                status = "expired"
            elif decision == "approve":
                if (self.checks.dependency_digest(tx, proposal.mutations)
                        != proposal.dependency_digest):
                    status = "target_changed"
                else:
                    try:
                        tx.apply(proposal.mutations, source=proposal.source,
                                 request_id=decision_id)
                    except api.LedgerError as exc:
                        # §22 E26: a live referrer added since the proposal (the digest does
                        # not cover referrers) ends it target_changed, reason has_dependents,
                        # instead of leaving it pending. apply is atomic, so only the
                        # receipt and its event are written. Every other refusal keeps its
                        # behaviour; ledger_integrity_failed is re-raised (§22 E22, §5.2).
                        if exc.code == "has_dependents":
                            reasons = ("has_dependents",)
                        elif exc.code not in {"target_changed", "lead_already_exists"}:
                            raise
                        status = "target_changed"
            receipt = tx.finish(proposal, status=status, receipt_request_id=decision_id,
                                applied=proposal.mutations if status == "committed" else (),
                                reason_codes=reasons)
            return self._result(tx, receipt, any_epoch=True)

    def _result(self, tx: PgLedgerTx, receipt: api.ReceiptRow | None, *,
                any_epoch: bool = False) -> api.ReceiptResult:
        if receipt is None:
            return api.ReceiptResult(False, None, None, (), tx.current_version, None)
        if receipt.epoch != tx.ctx.authority_epoch and not any_epoch:
            raise api.LedgerError("authority_changed")  # I12: never read across epochs
        proposal = tx.proposal(receipt.proposal_id)
        if proposal is None:
            raise integrity("receipt_without_proposal")
        records = []
        if receipt.status in {"committed", "target_changed"}:
            for mutation in proposal.mutations:
                record = tx.get(mutation.entity, mutation.record_id, include_deleted=True)
                if record is not None:
                    records.append((mutation.entity, record))
        undo = api.UndoView("none", None, None)
        if receipt.kind == "owner_direct" and receipt.status == "committed":
            # Derived, never stored: undone if an undo receipt names it, else by the clock.
            prior = tx.undo_receipt_of(receipt.request_id)
            if receipt.undo_until is None:
                raise integrity("receipt_undo_until")
            state = "undone" if prior else (
                "expired" if tx.now > receipt.undo_until else "available")
            undo = api.UndoView(state, receipt.undo_until, prior.request_id if prior else None)
        return api.ReceiptResult(True, receipt, proposal, tuple(records), tx.current_version,
                                 undo)

    def receipt(self, ctx: api.TrustedOwnerContext, request_id: str) -> api.ReceiptResult:
        """found=False means unknown; a receipt of another epoch is ``authority_changed``."""
        api.canonical_uuid(request_id)
        with self._read(ctx) as tx:
            return self._result(tx, tx.receipt(request_id))

    def creation_request(self, ctx: api.TrustedOwnerContext,
                         request_id: str) -> api.CreationResult:
        api.canonical_uuid(request_id)
        with self._read(ctx) as tx:
            proposal = tx.proposal_by_request(request_id)
            if proposal and proposal.epoch != ctx.authority_epoch:
                raise api.LedgerError("authority_changed")
            return api.CreationResult(
                bool(proposal), proposal,
                tx.receipt_for_proposal(proposal.proposal_id) if proposal else None,
                proposal.expires_at <= tx.now if proposal else None, tx.current_version)

    # ---- LedgerReader ----------------------------------------------------------------------

    def head(self, ctx: api.TrustedOwnerContext) -> api.Head:
        with self._read(ctx) as tx:
            verified = tx._exec(
                "SELECT verified_version FROM assaf_os.os_mirror_state "
                "WHERE workspace_id = :ws").scalar()
            return api.Head(tx.current_version, ctx.authority_epoch, dict(tx.states),
                            tx.pending_seq, int(verified or 0))

    def _window(self, tx: PgLedgerTx, listing: str, entity: str, after: str | None,
                limit: Any) -> int:
        if type(limit) is not int or not 1 <= limit <= 200:
            raise api.LedgerError("invalid_query")
        if after is None:
            return 0
        epoch, version, offset = self._cursors.decode(after, tx.ctx.workspace, listing, entity)
        if epoch != tx.ctx.authority_epoch or version != tx.current_version:
            raise api.LedgerError("cursor_invalidated",
                                  details={"observed_version": tx.current_version})
        return offset

    def _next(self, tx: PgLedgerTx, listing: str, entity: str, offset: int, limit: int,
              more: bool) -> str | None:
        if not more:
            return None
        return self._cursors.encode(tx.ctx.workspace, listing, entity, tx.ctx.authority_epoch,
                                    tx.current_version, offset + limit)

    def _page(self, ctx: api.TrustedOwnerContext, entity: Any, after: str | None, limit: Any,
              deleted: bool) -> api.Page:
        if entity not in api.ENTITIES:
            raise api.LedgerError("invalid_query")
        listing = "deleted" if deleted else "page"
        with self._read(ctx) as tx:
            offset = self._window(tx, listing, entity, after, limit)
            columns = ("record_id, revision, inputs, deleted_at"
                       + (", person_id" if entity == "leads" else ""))
            if deleted:
                order = 'deleted_at DESC, record_id COLLATE "C"'
            elif entity == "tasks":
                order = ('CASE WHEN status = ANY(:open) THEN 0 WHEN status = :done THEN 1 '
                         'ELSE 2 END, due_date ASC NULLS LAST, record_id COLLATE "C"')
            else:
                order = 'business_date DESC NULLS LAST, record_id COLLATE "C"'
            rows = tx._exec(
                f"SELECT {columns} FROM assaf_os.os_{entity} WHERE workspace_id = :ws AND "
                f"deleted_at IS {'NOT ' if deleted else ''}NULL ORDER BY {order} "
                "LIMIT :fetch OFFSET :offset",
                {"open": list(_OPEN_TASKS), "done": _DONE_TASK, "fetch": limit + 1,
                 "offset": offset}).all()
            total = int(tx._exec(
                f"SELECT count(*) FROM assaf_os.os_{entity} "
                "WHERE workspace_id = :ws AND deleted_at IS NULL").scalar())
            records = tuple(wire(entity, row) for row in rows[:limit])
            return api.Page(entity, records,
                            self._next(tx, listing, entity, offset, limit, len(rows) > limit),
                            total, tx.current_version)

    def page(self, ctx: api.TrustedOwnerContext, entity: api.Entity, *, after: str | None,
             limit: int) -> api.Page:
        return self._page(ctx, entity, after, limit, False)

    def deleted(self, ctx: api.TrustedOwnerContext, entity: api.Entity, *, after: str | None,
                limit: int) -> api.Page:
        return self._page(ctx, entity, after, limit, True)

    def pending(self, ctx: api.TrustedOwnerContext, *, after: str | None,
                limit: int) -> api.ProposalPage:
        """kind proposal, status pending, expires_at > the read's now, at the context epoch."""
        with self._read(ctx) as tx:
            offset = self._window(tx, "pending", "proposals", after, limit)
            rows = tx.pending_proposals(ctx.authority_epoch, limit + 1, offset)
            return api.ProposalPage(
                tuple(rows[:limit]),
                self._next(tx, "pending", "proposals", offset, limit, len(rows) > limit),
                tx.current_version)

    def labels(self, ctx: api.TrustedOwnerContext,
               refs: Iterable[api.RecordRef]) -> dict[str, dict[str, str]]:
        """D4: the first non-empty of name, title, category, cut to 120; none is omitted."""
        refs = list(refs)
        with self._read(ctx) as tx:
            found = tx.get_many(refs)
            result: dict[str, dict[str, str]] = {}
            for ref in refs:
                record = found.get(ref)
                if record is None:
                    continue
                label = next((str(record[k]) for k in ("name", "title", "category")
                              if record.get(k)), None)
                if label is not None:
                    result.setdefault(ref.entity + "." + api.ID_KEYS[ref.entity], {})[
                        ref.record_id] = label[:120]
            return result

    def monthly_summary(self, ctx: api.TrustedOwnerContext) -> tuple[dict[str, Any], ...]:
        with self._read(ctx) as tx:
            stored = tx._exec(
                "SELECT summary FROM assaf_os.os_summary_monthly WHERE workspace_id = :ws "
                "ORDER BY month_start").scalars().all()
            return monthly_rows(stored)


def build_ledger(settings: Any, *, clock: Callable[[], datetime] | None = None,
                 uuid4: Callable[[], str] = default_uuid4,
                 validator: api.StateValidator | None = None,
                 summaries: api.SummaryMaintainer | None = None,
                 checks: Any = None) -> PgLedger:
    """The PG ``api.Ledger`` on ``settings.owner_os_ledger_database_url``.

    ``ledger_unavailable`` when the ledger DSN or the main DSN is missing or not PostgreSQL. The
    switches (``assaf_os_ledger_enabled``, the kill switch, the backend, the epoch) are read at
    every call, not here, so the retention worker's ``purpose="expire"`` keeps working while the
    ledger is off (I11). V2-18 (the handoff): the ledger is ``reads.ReadModelLedger`` and the
    defaults are ``ScopedStateValidator`` and ``IncrementalSummaries``, which the V2-18 property
    tests prove equal to the full validator and the full summary recompute (pass those to opt
    out). V2-16b (§22 E23): the core checks default to ``tx.ScopedCoreChecks``, which
    ``test_core_scoping_property.py`` proves equal to V2-16's ``tx.FullCoreChecks``. ``clock`` is
    a test seam: by default ``tx.now`` is the database ``now()``.
    """
    dsn = getattr(settings, "owner_os_ledger_database_url", "") or ""
    main = getattr(settings, "database_url", "") or ""
    if not dsn:
        raise refuse("ledger_dsn_missing")
    if not main:
        raise refuse("main_dsn_missing")
    guard = guard_for(dsn, main)
    # The V2-18 handoff, imported here because reads.py imports this module.
    from app.services.os_ledger.reads import ReadModelLedger
    from app.services.os_ledger.summaries_incremental import IncrementalSummaries
    from app.services.os_ledger.validate_scoped import ScopedStateValidator

    return ReadModelLedger(
        settings, guard=guard, engine=engine_for(dsn), clock=clock, uuid4=uuid4,
        validator=validator if validator is not None else ScopedStateValidator(),
        summaries=summaries if summaries is not None else IncrementalSummaries(),
        checks=checks if checks is not None else ScopedCoreChecks())
