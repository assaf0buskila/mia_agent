"""V2-20: the ledger-backed OsBackend (V2_PG_CONTRACTS.md sections 7 and 10).

``get_os_backend`` (V2-17) imports ``PostgresOsBackend`` lazily and constructs it as
``PostgresOsBackend(settings, owner_id, actor=..., principal=...)`` under ``backend=postgres``.
Every method asks the ledger (``api.build_ledger``, V2-16) for a ledger answer and returns the
section 10.2 body, built by ``contract``. Three rules hold throughout:

* The ledger decides. This module never opens a business write of its own: a propose, a decision
  and a receipt are one ``LedgerCore`` call each, and a read is one or more ``LedgerReader`` calls.
* A body leaves only if the strict reader of ``contract`` accepts it. A ledger answer that breaks
  the contract is ``ledger_integrity_failed`` (503), never a half-right body.
* Logs carry an operation, an outcome, a reason code and a duration. Never a body, a record, an id.

The trusted owner context comes from V2-19's ``authorize`` module, resolved lazily through the
``contexts`` seam. A deployment without it fails closed with 503 ``ledger_unavailable``.
"""

from __future__ import annotations

import contextlib
import importlib
import logging
import re
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol, cast

from app.services.os_ledger import api, contract

_LOG = logging.getLogger("mia.os_backend_pg")
_AUTHORIZE_MODULE = "app.services.os_ledger.authorize"
_ACTORS = ("OS", "MIA", "TELEGRAM_APPROVAL")
_OWNER = re.compile(r"[1-9][0-9]{0,19}")
_TOKEN = re.compile(r"[\x21-\x7e]+")

# The section 10.2 query allow-list.
MIN_LIMIT, MAX_LIMIT, DEFAULT_LIMIT = 1, 200, 50
MAX_AFTER_CHARS = 512
MAX_SINCE_CHARS = 64
# ``pending_count`` pages through the pending list; a very long list is refused, not guessed.
PENDING_COUNT_PAGE = 200
PENDING_COUNT_MAX_PAGES = 50


def _invalid_query() -> api.LedgerError:
    return api.LedgerError("invalid_query")


def _checked_page_args(after: Any, limit: Any) -> None:
    if type(limit) is not int or not MIN_LIMIT <= limit <= MAX_LIMIT:
        raise _invalid_query()
    if after is not None and (
        type(after) is not str or len(after) > MAX_AFTER_CHARS or not _TOKEN.fullmatch(after)
    ):
        raise _invalid_query()


def _checked_entity(entity: Any) -> str:
    if type(entity) is not str or entity not in api.ENTITIES:
        raise _invalid_query()
    return entity


def _checked_query(query: Any) -> tuple[str, str | None, int, str | None]:
    entity = _checked_entity(getattr(query, "entity", "tasks"))
    after = getattr(query, "after", None)
    limit = getattr(query, "limit", DEFAULT_LIMIT)
    since = getattr(query, "since", None)
    _checked_page_args(after, limit)
    if since is not None and (
        type(since) is not str or len(since) > MAX_SINCE_CHARS or not _TOKEN.fullmatch(since)
    ):
        raise _invalid_query()
    if after is not None and since is not None:
        raise _invalid_query()
    return entity, after, limit, since


@dataclass(frozen=True)
class PageQuery:
    """The validated section 10.2 query (``entity``, ``after``, ``limit`` and ``since``).

    ``PostgresOsBackend.snapshot`` accepts any object with these four attributes (V2-17's
    ``SnapshotQuery`` is one), and validates them again, so a caller can never skip the allow-list.
    """

    entity: str = "tasks"
    after: str | None = None
    limit: int = DEFAULT_LIMIT
    since: str | None = None

    def __post_init__(self) -> None:
        _checked_query(self)


# ---------------------------------------------------------------------------------------------
# The trusted owner context seam (section 9)


class ContextSource(Protocol):
    """Where the backend gets the ``TrustedOwnerContext`` of each kind of call."""

    def for_read(self) -> api.TrustedOwnerContext: ...

    def for_propose(self) -> api.TrustedOwnerContext: ...

    def for_decide(self) -> api.TrustedOwnerContext: ...


def _load_authorize() -> Any:
    try:
        return importlib.import_module(_AUTHORIZE_MODULE)
    except ImportError:
        _LOG.warning("os_backend_pg op=authorize outcome=refused reason=authorize_missing")
        raise api.LedgerError("ledger_unavailable", reason="authorize_missing") from None


class AuthorizeContexts:
    """V2-19's constructors, chosen by actor (the section 9 table), loaded on first use.

    ``principal`` is what the channel entry point authenticated: the session principal for OS, the
    tool principal for MIA, the ApprovalRow for a Telegram decision. It is passed through
    untouched. The constructors re-check the numeric owner, the scopes and the mutation proof.
    """

    def __init__(self, settings: Any, actor: str, principal: Any) -> None:
        self._settings, self._actor, self._principal = settings, actor, principal

    def for_read(self) -> api.TrustedOwnerContext:
        if self._actor == "OS":
            return _load_authorize().from_owner_read(self._principal, self._settings)
        if self._actor == "MIA":
            return _load_authorize().from_mia_tool(self._principal, self._settings)
        raise api.LedgerError("channel_operation_denied")

    def for_propose(self) -> api.TrustedOwnerContext:
        if self._actor == "OS":
            return _load_authorize().from_owner_mutation(self._principal, self._settings)
        if self._actor == "MIA":
            return _load_authorize().from_mia_tool(self._principal, self._settings)
        raise api.LedgerError("channel_operation_denied")

    def for_decide(self) -> api.TrustedOwnerContext:
        if self._actor == "OS":
            return _load_authorize().from_owner_mutation(self._principal, self._settings)
        if self._actor == "TELEGRAM_APPROVAL":
            return _load_authorize().from_telegram_decision(self._principal, self._settings)
        raise api.LedgerError("owner_approval_required")


# ---------------------------------------------------------------------------------------------
# The backend


class PostgresOsBackend:
    """The ``OsBackend`` protocol (section 7) over the PostgreSQL ledger.

    ``ledger``, ``contexts``, ``clock`` and ``mirror`` are test and integration seams. By default
    the ledger is ``api.build_ledger(settings)``, the contexts are V2-19's constructors, the clock
    is the wall clock, and the ``mirror`` view is derived from the ledger head alone (V2-29 injects
    the full view, with ``last_verified_at`` and ``lag_s``).
    """

    kind: Literal["postgres"] = "postgres"

    def __init__(
        self,
        settings: Any,
        owner_id: str,
        *,
        actor: str,
        principal: Any = None,
        ledger: api.Ledger | None = None,
        contexts: ContextSource | None = None,
        clock: Callable[[], datetime] | None = None,
        mirror: Callable[[api.Head], Mapping[str, Any]] | None = None,
    ) -> None:
        if getattr(settings, "assaf_os_backend", "sheets") != "postgres":
            raise api.LedgerError("backend_not_postgres")
        if (
            type(owner_id) is not str or _OWNER.fullmatch(owner_id) is None
            or owner_id not in settings.telegram_owner_user_id_set()
        ):
            raise api.LedgerError("operation_unavailable")
        if actor not in _ACTORS:
            raise api.LedgerError("channel_operation_denied")
        self._settings, self._actor = settings, actor
        self._ledger = ledger if ledger is not None else api.build_ledger(settings)
        self._contexts = contexts if contexts is not None else AuthorizeContexts(
            settings, actor, principal)
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._mirror = mirror

    # ---- plumbing --------------------------------------------------------------------------

    @contextlib.contextmanager
    def _operation(self, op: str) -> Iterator[None]:
        started = time.monotonic()

        def log(outcome: str, reason: str) -> None:
            _LOG.info(
                "os_backend_pg op=%s outcome=%s reason=%s ms=%d",
                op, outcome, reason, int((time.monotonic() - started) * 1000),
            )

        try:
            yield
        except api.LedgerError as exc:
            log("refused", exc.reason or exc.code)
            raise
        except contract.ContractError as exc:
            reason = "contract_" + re.sub(r"[^a-z0-9_]", "_", exc.reason)[:48]
            log("refused", reason)
            raise api.LedgerError("ledger_integrity_failed", reason=reason) from None
        log("ok", "none")

    @staticmethod
    def _trusted(ctx: Any) -> api.TrustedOwnerContext:
        if not isinstance(ctx, api.TrustedOwnerContext):
            raise api.LedgerError("invalid_trusted_context")
        return ctx

    def _read_ctx(self) -> api.TrustedOwnerContext:
        return self._trusted(self._contexts.for_read())

    def _mirror_view(self, head: api.Head) -> dict[str, Any]:
        if self._mirror is not None:
            return dict(self._mirror(head))
        verified = head.mirror_verified_version
        if not getattr(self._settings, "assaf_os_mirror_enabled", False):
            state = "disabled"
        elif verified == head.observed_version:
            state = "verified"
        elif verified == 0:
            state = "unknown"
        else:
            state = "lagging"
        return contract.mirror_view(state, last_verified_at=None, verified_version=verified,
                                    lag_s=None)

    def _pending_count(self, ctx: api.TrustedOwnerContext) -> int:
        total, after = 0, None
        for _ in range(PENDING_COUNT_MAX_PAGES):
            page = self._ledger.pending(ctx, after=after, limit=PENDING_COUNT_PAGE)
            total += len(page.items)
            if page.next_cursor is None:
                return total
            after = page.next_cursor
        raise api.LedgerError("ledger_unavailable", reason="pending_overflow")

    def _receipt_body(
        self, result: api.ReceiptResult, ctx: api.TrustedOwnerContext, request_id: str
    ) -> dict[str, Any]:
        return contract.read_receipt_v2(contract.receipt_v2(
            result, workspace_id=ctx.workspace, authority_epoch=ctx.authority_epoch,
            request_id=request_id, server_now=self._clock()))

    # ---- reads -----------------------------------------------------------------------------

    def schema(self) -> dict[str, Any]:
        with self._operation("schema"):
            ctx = self._read_ctx()
            # No other read happens here, so ask the ledger for the epoch it really holds.
            head = self._ledger.head(ctx)
            return contract.read_schema_v2(
                contract.schema_body(ctx.workspace, head.authority_epoch))

    def snapshot(self, query: Any = None) -> dict[str, Any]:
        entity, after, limit, since = _checked_query(query if query is not None else PageQuery())
        token = api.parse_since(since) if since is not None else None
        with self._operation("snapshot"):
            ctx = self._read_ctx()
            # Order matters. The head is read first, so the ``since`` token is never newer than
            # the data: a change in between shows on the next poll. Every other read comes after
            # the page, so none of it is older than the ``observed_version`` the body claims.
            head = self._ledger.head(ctx)
            mirror = self._mirror_view(head)
            current = api.since_token(head)
            if token == (head.observed_version, head.pending_seq, head.mirror_verified_version):
                return contract.read_snapshot_v2(contract.unchanged_body(
                    workspace_id=ctx.workspace, authority_epoch=head.authority_epoch,
                    observed_version=head.observed_version, since=current, mirror=mirror))
            page = self._ledger.page(ctx, cast(api.Entity, entity), after=after, limit=limit)
            monthly = self._ledger.monthly_summary(ctx)
            refs = contract.label_refs(entity, page.records)
            labels = self._ledger.labels(ctx, refs) if refs else {}
            pending_count = self._pending_count(ctx)
            return contract.read_snapshot_v2(contract.page_body(
                workspace_id=ctx.workspace, authority_epoch=head.authority_epoch,
                observed_version=page.observed_version, observed_at=self._clock(), since=current,
                entity=entity, records=page.records, next_cursor=page.next_cursor,
                total=page.total, summary=contract.summary_view(monthly),
                pending_count=pending_count, labels=labels, mirror=mirror))

    def pending(self, *, after: str | None = None, limit: int = DEFAULT_LIMIT) -> dict[str, Any]:
        """``GET /pending``: the pending ``proposal``-kind items, newest ledger view."""
        _checked_page_args(after, limit)
        with self._operation("pending"):
            ctx = self._read_ctx()
            page = self._ledger.pending(ctx, after=after, limit=limit)
            return contract.read_pending_list(contract.pending_body(
                workspace_id=ctx.workspace, authority_epoch=ctx.authority_epoch,
                observed_version=page.observed_version, next_cursor=page.next_cursor,
                items=[contract.proposal_v2(row, authority_epoch=ctx.authority_epoch)
                       for row in page.items]))

    def deleted(
        self, entity: str, *, after: str | None = None, limit: int = DEFAULT_LIMIT
    ) -> dict[str, Any]:
        """``GET /deleted``: tombstoned rows of one entity, most recently deleted first."""
        _checked_page_args(after, limit)
        _checked_entity(entity)
        with self._operation("deleted"):
            ctx = self._read_ctx()
            page = self._ledger.deleted(ctx, cast(api.Entity, entity), after=after, limit=limit)
            return contract.read_deleted_page(contract.deleted_body(
                workspace_id=ctx.workspace, authority_epoch=ctx.authority_epoch,
                observed_version=page.observed_version, entity=entity, records=page.records,
                next_cursor=page.next_cursor))

    def receipt(self, request_id: str) -> dict[str, Any]:
        with self._operation("receipt"):
            ctx = self._read_ctx()
            result = self._ledger.receipt(ctx, request_id)
            return self._receipt_body(result, ctx, request_id)

    def proposal_request(self, request_id: str) -> dict[str, Any]:
        """Recover a lost creation acknowledgement by reading (I1): never by re-posting."""
        with self._operation("proposal_request"):
            ctx = self._read_ctx()
            found = self._ledger.creation_request(ctx, request_id)
            proposal = receipt = None
            if found.found and found.proposal is not None:
                proposal = contract.proposal_v2(found.proposal, authority_epoch=ctx.authority_epoch)
                if found.decision is not None:
                    answer = self._ledger.receipt(ctx, found.decision.request_id)
                    if not answer.found:
                        raise api.LedgerError(
                            "ledger_integrity_failed", reason="decision_without_receipt")
                    receipt = self._receipt_body(answer, ctx, found.decision.request_id)
            return contract.read_proposal_request_v2(contract.proposal_request_body(
                workspace_id=ctx.workspace, authority_epoch=ctx.authority_epoch,
                observed_version=found.observed_version, request_id=request_id,
                proposal=proposal, receipt=receipt))

    def proposal(self, proposal_id: str, *, budget: float | None = None) -> dict[str, Any]:
        """``GET /proposals/{id}``: any ``proposal``-kind row; every other kind is 404."""
        del budget  # the ledger has no remote call to bound
        with self._operation("proposal"):
            ctx = self._read_ctx()
            # LedgerReader has no proposal-by-id yet, so this read is admitted as a write
            # transaction: it fails closed under the kill switch and a fenced core (I8), and on PG
            # it takes the workspace lock. A read-only API from V2-16/V2-18 replaces it.
            with self._ledger.transaction(ctx, domain="core", kind="proposal") as tx:
                row = tx.proposal(proposal_id)
            if row is None or row.kind != "proposal":
                raise api.LedgerError("proposal_not_found")
            if row.epoch != ctx.authority_epoch:
                raise api.LedgerError("authority_changed")  # I12: never read across epochs
            return contract.read_proposal_v2(
                contract.proposal_v2(row, authority_epoch=ctx.authority_epoch),
                statuses=contract.PROPOSAL_STATUSES)

    # ---- writes ----------------------------------------------------------------------------

    def propose(self, request: Any) -> dict[str, Any]:
        with self._operation("propose"):
            ctx = self._trusted(self._contexts.for_propose())
            row = self._ledger.propose(ctx, api.ProposeInput(
                request_id=request.request_id, entity=request.entity,
                record_id=request.record_id, expected_revision=request.expected_revision,
                input=dict(request.input)))
            # A replay returns the stored row whatever its status (I1), so the guard names them.
            return contract.read_proposal_v2(
                contract.proposal_v2(row, authority_epoch=ctx.authority_epoch),
                statuses=contract.PROPOSAL_STATUSES)

    def decide(
        self, operation: Literal["approve", "reject"], proposal_id: str, request: Any
    ) -> dict[str, Any]:
        if operation not in ("approve", "reject"):
            raise api.LedgerError("validation_error", details={"reason": "invalid_decision"})
        if self._actor == "MIA":
            raise api.LedgerError("owner_approval_required")  # I5: the model never decides
        with self._operation("decide"):
            ctx = self._trusted(self._contexts.for_decide())
            result = self._ledger.decide(ctx, proposal_id, request.request_id, operation)
            return self._receipt_body(result, ctx, request.request_id)


__all__ = ["AuthorizeContexts", "ContextSource", "PageQuery", "PostgresOsBackend"]
