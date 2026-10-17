"""Frozen ledger API (os-ledger.v1): names, value types and protocols shared by every v2 chunk.

The contract is ``V2_PG_CONTRACTS.md`` §5.1 (FROZEN v1). A change to a name or a signature here is a
fork, never a local edit. V2-16 implements the protocols on PostgreSQL; ``tests/fakes/os_ledger.py``
(V2-16f) implements them in memory. Nothing in this module touches a database at import time.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import date, datetime
from types import MappingProxyType
from typing import Any, Literal, NamedTuple, Protocol
from uuid import NAMESPACE_URL, RFC_4122, UUID, uuid5

LEDGER_API = "os-ledger.v1"
ENTITIES = ("leads", "clients", "works", "payments", "time", "expenses", "tasks")
# Literal copy; tests/unit/test_os_v2_scaffold.py pins it to app.prototypes.assaf_os_domain.ID_KEYS.
ID_KEYS: Mapping[str, str] = MappingProxyType(
    {
        "leads": "lead_id",
        "clients": "person_id",
        "works": "work_id",
        "payments": "payment_id",
        "time": "time_id",
        "expenses": "expense_id",
        "tasks": "task_id",
    }
)

Entity = Literal["leads", "clients", "works", "payments", "time", "expenses", "tasks"]
Domain = Literal["core", "statements"]
AuthorityState = Literal["shadow", "active", "fenced", "restoring"]
Channel = Literal["OS", "MIA", "TELEGRAM_APPROVAL", "ASSISTANT_CLAUDE", "ASSISTANT_GPT", "WORKER"]
Source = Literal[
    "OS", "OS_DIRECT", "MIA", "TELEGRAM_APPROVAL",
    "ASSISTANT_CLAUDE", "ASSISTANT_GPT", "ASSISTANT_PASTE", "IMPORT",
]
ProposalKind = Literal["proposal", "owner_direct", "owner_undo", "statement_batch", "import"]
ProposalStatus = Literal[
    "pending", "committed", "rejected", "expired", "target_changed", "superseded", "abandoned",
]
ReceiptStatus = Literal[
    "committed", "rejected", "expired", "target_changed", "superseded", "abandoned",
]
Operation = Literal[
    "read", "read_rules", "read_own_batches", "propose", "propose_statement_batch",
    "decide", "decide_statement_batch", "draft_statement_batch", "commit_direct",
    "undo", "abandon", "manage_credentials", "import_promote", "authority_admin",
    "expire_batches",
]
RecordDict = dict[str, Any]  # the v1 wire record: schema fields + id key + "revision"
# Erratum E1-rev (§22): the import batch status machine, in V2-14's own vocabulary. staged ->
# validated (manifest computed, no quarantined row) -> imported (only inside promote); staged ->
# quarantined (terminal); staged|validated -> failed (abandon).
ImportStatus = Literal["staged", "validated", "imported", "quarantined", "failed"]
IMPORT_STATUS_EDGES: frozenset[tuple[str, str]] = frozenset({
    ("staged", "validated"),
    ("staged", "quarantined"),
    ("validated", "imported"),
    ("staged", "failed"),
    ("validated", "failed"),
})
LIVE_IDS_LIMIT = 100_000  # live_ids() refuses with local_record_limit above this, per entity

_LOG = logging.getLogger("mia.os_ledger")


# ---------------------------------------------------------------------------------------------
# Errors (§5.2). This table is the complete code list; each code has exactly one HTTP status.


class ErrorSpec(NamedTuple):
    status: int
    retryable: bool
    details: frozenset[str]  # the only keys ``details`` may carry; empty means details is absent


def _spec(status: int, retryable: bool = False, *details: str) -> ErrorSpec:
    return ErrorSpec(status, retryable, frozenset(details))


ERROR_CODES: Mapping[str, ErrorSpec] = MappingProxyType({
    "invalid_query": _spec(400),
    "owner_auth_required": _spec(401),
    "assistant_auth_required": _spec(401),
    "kill_switch": _spec(403),
    "scope_denied": _spec(403),
    "channel_operation_denied": _spec(403),
    "owner_approval_required": _spec(403),
    "browser_mutation_verification_required": _spec(403),
    "step_up_required": _spec(403),
    "invalid_trusted_context": _spec(403),
    "operation_unavailable": _spec(404),
    "proposal_not_found": _spec(404),
    "commit_not_found": _spec(404),
    "batch_not_found": _spec(404),
    "idempotency_mismatch": _spec(409),
    "decision_request_mismatch": _spec(409),
    "already_decided": _spec(409),
    "authority_changed": _spec(409),
    "authority_moved": _spec(409),
    "cursor_invalidated": _spec(409, False, "observed_version"),
    "target_changed": _spec(409, False, "reason"),
    "not_undoable": _spec(409),
    "already_undone": _spec(409, False, "undo_request_id"),
    "undo_expired": _spec(409),
    "undo_conflict": _spec(409, False, "reason", "changed"),
    "has_dependents": _spec(409, False, "dependents"),
    "review_stale": _spec(409),
    "unassigned_lines": _spec(409),
    "merge_choice_required": _spec(409),
    "draft_conflict": _spec(409),
    "batch_conflict": _spec(409, False, "conflicting_proposal_id"),
    "lead_already_exists": _spec(409),
    "import_quarantine_unresolved": _spec(409, False, "count"),
    "manifest_digest_mismatch": _spec(409),
    "import_orphan_rows": _spec(409, False, "counts"),
    # Erratum E1-rev (§22): the import LedgerTx methods.
    "invalid_transaction_kind": _spec(409),
    "import_status_invalid": _spec(409),
    "local_record_limit": _spec(409),
    "invalid_entity": _spec(422),
    "validation_error": _spec(422, False, "reason"),
    "invalid_request_id": _spec(422),
    "invalid_cursor": _spec(422),
    "invalid_statement_batch": _spec(422, False, "issues"),
    "rate_limited": _spec(429, True),
    "ledger_unavailable": _spec(503, True),
    "backend_not_postgres": _spec(503),
    "authority_not_active": _spec(503),
    "os_frozen": _spec(503, True),
    "ledger_integrity_failed": _spec(503),
    "unknown": _spec(503),
})
_REASON_RE = re.compile(r"[a-z][a-z0-9_]{0,63}")


class LedgerError(Exception):
    """Every ledger refusal. ``reason`` is a log-only reason code and is never rendered."""

    code: str
    status: int
    details: Mapping[str, Any]
    write_outcome: Literal["not_started", "unknown"]
    request_id: str | None
    decision_id: str | None
    reason: str | None

    def __init__(
        self,
        code: str,
        status: int | None = None,
        *,
        details: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        decision_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        spec = ERROR_CODES.get(code)
        if spec is None:
            raise ValueError("ledger_error_code_unknown")
        if status is not None and status != spec.status:
            raise ValueError("ledger_error_status_mismatch")
        details = dict(details or {})
        if not set(details) <= spec.details:
            raise ValueError("ledger_error_details_not_allowed")
        if reason is not None and not _REASON_RE.fullmatch(reason):
            reason = "error"
        super().__init__(code)
        self.code = code
        self.status = spec.status
        self.details = MappingProxyType(details)
        self.write_outcome = "unknown" if code == "unknown" else "not_started"
        self.request_id = request_id
        self.decision_id = decision_id
        self.reason = reason

    @property
    def retryable(self) -> bool:
        return ERROR_CODES[self.code].retryable


# ---------------------------------------------------------------------------------------------
# Digests and ids


def digest(value: Any) -> str:
    """sha256 hex of the canonical JSON encoding; raises on NaN, infinities and non-JSON values."""
    encoded = json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_uuid(value: str, *, version: int | None = None) -> str:
    """Return ``value`` only when it is already the canonical lowercase hyphenated UUID."""
    try:
        if type(value) is not str or len(value) != 36:
            raise ValueError
        parsed = UUID(value)
        if str(parsed) != value:
            raise ValueError
        if version is not None and (parsed.version != version or parsed.variant != RFC_4122):
            raise ValueError
    except (AttributeError, TypeError, ValueError):
        raise LedgerError("invalid_request_id") from None
    return value


REQUEST_NS = uuid5(NAMESPACE_URL, "https://mia.assafweb.com/ns/assaf-os/request/v1")


def mia_request_uuid(owner_id: str, workspace_id: str, source_id: str) -> str:
    return str(uuid5(REQUEST_NS, f"{owner_id}:{workspace_id}:{source_id}"))


def paste_request_uuid(owner_id: str, client_batch_id: str) -> str:
    return str(uuid5(REQUEST_NS, f"paste:{owner_id}:{client_batch_id}"))


def assistant_request_uuid(credential_id: str, client_batch_id: str) -> str:
    return str(uuid5(REQUEST_NS, f"{credential_id}:{client_batch_id}"))


def import_request_uuid(epoch: int, manifest_digest: str) -> str:
    return str(uuid5(REQUEST_NS, f"import:{epoch}:{manifest_digest}"))


_SINCE_RE = re.compile(r"[0-9]{1,19}\.[0-9]{1,19}\.[0-9]{1,19}")


def since_token(head: Head) -> str:
    """Opaque to clients: ``<observed_version>.<pending_seq>.<mirror_verified_version>``."""
    return f"{head.observed_version}.{head.pending_seq}.{head.mirror_verified_version}"


def parse_since(token: str) -> tuple[int, int, int]:
    if type(token) is not str or not _SINCE_RE.fullmatch(token):
        raise LedgerError("invalid_query")
    version, pending_seq, mirror_version = (int(part) for part in token.split("."))
    return version, pending_seq, mirror_version


# ---------------------------------------------------------------------------------------------
# Trusted owner context. Only os_ledger/authorize.py (V2-19) and tests/fakes/contexts.py
# call _issue.

_CAPABILITY = object()


@dataclass(frozen=True)
class TrustedOwnerContext:
    numeric_owner: int
    workspace: str
    authority_epoch: int
    channel: Channel
    scopes: frozenset[str]
    auth_expires_at: datetime
    browser_mutation_verified: bool
    credential_id: str | None
    _capability: object = field(repr=False)

    def __post_init__(self) -> None:
        if self._capability is not _CAPABILITY:
            raise LedgerError("invalid_trusted_context", 403)


def _issue(**fields: Any) -> TrustedOwnerContext:
    return TrustedOwnerContext(**fields, _capability=_CAPABILITY)


# ---------------------------------------------------------------------------------------------
# Value types


@dataclass(frozen=True)
class RecordRef:
    entity: Entity
    record_id: str


@dataclass(frozen=True)
class Mutation:
    entity: Entity
    record_id: str
    op: Literal["upsert", "tombstone", "restore"]
    before: Mapping[str, Any] | None  # current wire record when built (None = create)
    # Full wire record. after["revision"] == (before or {revision: 0})["revision"] + 1, except
    # in a kind="import" transaction, where it is the manifest revision: >= 1 on a create and
    # greater than the current revision on an update (§5.3).
    after: Mapping[str, Any]


@dataclass(frozen=True)
class ProposalDraft:
    kind: ProposalKind
    source: Source
    request_id: str
    request_digest: str
    entity: str  # Entity | "promote_lead" | "statements"
    record_id: str  # target id; statement_id for a batch; first item's id for a bulk
    expected_revision: int
    mutations: tuple[Mutation, ...]
    dependency_digest: str
    expires_at: datetime
    credential_id: str | None = None


@dataclass(frozen=True)
class ProposalRow:  # every os_proposals column, ISO datetimes
    workspace_id: str
    proposal_id: str
    request_id: str
    request_digest: str
    envelope_digest: str
    owner_id: str
    epoch: int
    approval_id: str
    rejection_id: str
    entity: str
    record_id: str
    expected_revision: int
    mutations: tuple[Mutation, ...]
    dependency_digest: str
    source: Source
    credential_id: str | None
    kind: ProposalKind
    status: ProposalStatus
    created_at: datetime
    expires_at: datetime


@dataclass(frozen=True)
class ReceiptRow:
    workspace_id: str
    request_id: str
    proposal_id: str
    epoch: int
    status: ReceiptStatus
    kind: ProposalKind
    commit_version: int
    records: tuple[Mapping[str, Any], ...]  # {entity, record_id, revision, digest}
    completed_at: datetime
    undo_until: datetime | None
    undo_of: str | None
    # <= 20, e.g. ("credential_revoked",), ("restored_without_detail",)
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class UndoView:
    state: Literal["available", "undone", "expired", "none"]
    until: datetime | None
    undo_request_id: str | None


@dataclass(frozen=True)
class ReceiptResult:
    found: bool
    receipt: ReceiptRow | None
    proposal: ProposalRow | None
    records: tuple[tuple[Entity, RecordDict], ...]  # current records for committed/target_changed
    observed_version: int
    undo: UndoView | None


@dataclass(frozen=True)
class CreationResult:
    found: bool
    proposal: ProposalRow | None
    decision: ReceiptRow | None
    past_expiry: bool | None
    observed_version: int


@dataclass(frozen=True)
class ProposeInput:  # same validation as app.services.assaf_os.ProposeRequest
    request_id: str
    entity: str
    record_id: str | None
    expected_revision: int
    input: Mapping[str, Any]
    approval_expires_at: datetime | None = None


@dataclass(frozen=True)
class AuthoritySnapshot:
    workspace_id: str
    epoch: int
    version: int
    states: Mapping[Domain, AuthorityState]
    activated_at: Mapping[Domain, datetime | None]


@dataclass(frozen=True)
class Head:
    observed_version: int
    authority_epoch: int
    states: Mapping[Domain, AuthorityState]
    pending_seq: int
    mirror_verified_version: int


@dataclass(frozen=True)
class Page:
    entity: Entity
    records: tuple[RecordDict, ...]
    next_cursor: str | None
    total: int
    observed_version: int


@dataclass(frozen=True)
class ImportRow:  # one os_import_rows insert (erratum E1-rev); rows are insert-only
    row_number: int  # > 0, unique within the batch
    entity: Entity
    record_id: str | None  # None for an id-less row, which is always quarantined
    source_digest: str  # unique within the batch
    status: Literal["staged", "quarantined"]
    raw_record: Mapping[str, Any]
    canonical_record: Mapping[str, Any] | None
    provenance: Mapping[str, Any]
    quarantine: Mapping[str, Any] | None  # required when status == "quarantined"


@dataclass(frozen=True)
class ProposalPage:
    items: tuple[ProposalRow, ...]
    next_cursor: str | None
    observed_version: int


# ---------------------------------------------------------------------------------------------
# Protocols


class LedgerTx(Protocol):
    ctx: TrustedOwnerContext
    now: datetime  # DB now() at begin
    version: int  # version at lock
    # PG: this transaction's SQLAlchemy Connection; fake: None. Use ONLY for chunk-owned tables
    # (§4 statements/assistant), never for the V2-14 core tables.
    connection: Any
    # Fake: per-workspace store rolled back with the tx (in-memory chunk tables); PG: {}.
    scratch: dict[str, Any]

    def get(
        self, entity: Entity, record_id: str, *, include_deleted: bool = False
    ) -> RecordDict | None: ...
    def get_many(
        self, refs: Sequence[RecordRef], *, include_deleted: bool = False
    ) -> dict[RecordRef, RecordDict]: ...
    def find_expenses(
        self,
        *,
        ids: Sequence[str] = (),
        source_key_prefix: str | None = None,
        manual_window: tuple[date, date] | None = None,
    ) -> list[RecordDict]: ...
    # DEPENDENTS edges whose referrer is_live()
    def dependents(self, entity: Entity, record_id: str) -> list[RecordRef]: ...
    def proposal(self, proposal_id: str) -> ProposalRow | None: ...
    def proposal_by_request(self, request_id: str) -> ProposalRow | None: ...
    def receipt(self, request_id: str) -> ReceiptRow | None: ...
    def receipt_for_proposal(self, proposal_id: str) -> ReceiptRow | None: ...
    def undo_receipt_of(self, request_id: str) -> ReceiptRow | None: ...
    # Status pending; bumps pending_seq.
    def insert_proposal(self, draft: ProposalDraft) -> ProposalRow: ...
    def apply(
        self, mutations: Sequence[Mutation], *, source: Source, request_id: str
    ) -> tuple[RecordDict, ...]: ...
    def finish(
        self,
        proposal: ProposalRow,
        *,
        status: ReceiptStatus,
        receipt_request_id: str,
        applied: Sequence[Mutation] = (),
        undo_until: datetime | None = None,
        undo_of: str | None = None,
        reason_codes: Sequence[str] = (),
    ) -> ReceiptRow: ...  # bumps version + pending_seq

    # Erratum E1-rev (§22). The three writers run only in a kind="import" transaction with
    # core=shadow; live_ids also runs in reads. Any other use raises invalid_transaction_kind. Rows
    # are insert-only, and a status change outside IMPORT_STATUS_EDGES raises import_status_invalid.
    # set_import_status sets completed_at exactly on entering imported, quarantined or failed, and
    # needs a quarantine object on entering quarantined.
    def insert_import_batch(
        self,
        *,
        batch_id: str,
        authority_epoch: int,
        source_digest: str,
        source_binding: str,
        provenance: Mapping[str, Any],
    ) -> None: ...
    def insert_import_rows(self, batch_id: str, rows: Sequence[ImportRow]) -> int: ...
    def set_import_status(
        self, batch_id: str, status: ImportStatus, *, quarantine: Mapping[str, Any] | None = None
    ) -> None: ...
    # Non-deleted, non-cancelled ids; invalid_entity for a non-record entity; local_record_limit
    # above LIVE_IDS_LIMIT.
    def live_ids(self, entity: Entity) -> frozenset[str]: ...


class LedgerStore(Protocol):
    # purpose="expire": kind statement_batch, WORKER only (§5.3).
    def transaction(
        self,
        ctx: TrustedOwnerContext,
        *,
        domain: Domain,
        kind: ProposalKind,
        purpose: Literal["write", "expire"] = "write",
    ) -> AbstractContextManager[LedgerTx]: ...


class LedgerCore(Protocol):
    def propose(self, ctx: TrustedOwnerContext, req: ProposeInput) -> ProposalRow: ...
    # kind "proposal" ONLY; any other kind is 404 proposal_not_found.
    def decide(
        self,
        ctx: TrustedOwnerContext,
        proposal_id: str,
        decision_id: str,
        decision: Literal["approve", "reject"],
    ) -> ReceiptResult: ...
    # found=False == "unknown"
    def receipt(self, ctx: TrustedOwnerContext, request_id: str) -> ReceiptResult: ...
    def creation_request(self, ctx: TrustedOwnerContext, request_id: str) -> CreationResult: ...


class LedgerReader(Protocol):  # V2-18 implements on PG
    def head(self, ctx: TrustedOwnerContext) -> Head: ...
    def page(
        self, ctx: TrustedOwnerContext, entity: Entity, *, after: str | None, limit: int
    ) -> Page: ...
    def deleted(
        self, ctx: TrustedOwnerContext, entity: Entity, *, after: str | None, limit: int
    ) -> Page: ...
    # kind proposal, status pending, expires_at > read-tx now()
    def pending(
        self, ctx: TrustedOwnerContext, *, after: str | None, limit: int
    ) -> ProposalPage: ...
    # {"clients.person_id": {id: label}}
    def labels(
        self, ctx: TrustedOwnerContext, refs: Iterable[RecordRef]
    ) -> dict[str, dict[str, str]]: ...
    # The v1 monthly row shape, unchanged.
    def monthly_summary(self, ctx: TrustedOwnerContext) -> tuple[dict[str, Any], ...]: ...


class AuthorityAdmin(Protocol):  # privileged DSN, CLI jobs only
    # First call: epoch must be 1, both domains shadow; an equal repeat is a no-op.
    def ensure_workspace(
        self, workspace_id: str, owner_id: str, epoch: int
    ) -> AuthoritySnapshot: ...
    def state(self, workspace_id: str) -> AuthoritySnapshot: ...
    def transition(
        self,
        workspace_id: str,
        domain: Domain,
        to: AuthorityState,
        *,
        expected_epoch: int,
        actor_owner_id: str,
    ) -> AuthoritySnapshot: ...
    def bump_epoch(self, workspace_id: str, *, expected_epoch: int) -> AuthoritySnapshot: ...
    # Write-once; equal = no-op; different = LedgerError("ledger_unavailable"),
    # reason statement_pepper_mismatch.
    def pin_statement_pepper(self, workspace_id: str, fingerprint: str) -> AuthoritySnapshot: ...


class StateValidator(Protocol):
    # Raises target_changed with details.reason.
    def validate(self, tx: LedgerTx, applied: Sequence[Mutation]) -> None: ...


class SummaryMaintainer(Protocol):
    def on_apply(self, tx: LedgerTx, applied: Sequence[Mutation]) -> None: ...


class Ledger(LedgerStore, LedgerCore, LedgerReader, Protocol): ...


# ---------------------------------------------------------------------------------------------
# Dependencies (FORK-DELETE). The one table; FakeLedger and PG both import it. Mirrors every
# V2-14 FK. Each edge is (referrer entity, referrer column, referenced entity).

DEPENDENTS: tuple[tuple[Entity, str, Entity], ...] = (
    ("works", "person_id", "clients"),
    ("payments", "person_id", "clients"),
    ("time", "person_id", "clients"),
    ("tasks", "person_id", "clients"),
    ("payments", "work_id", "works"),
    ("time", "work_id", "works"),
    ("tasks", "work_id", "works"),
    ("payments", "original_payment_id", "payments"),
)

# FORK-DELETE end states (§0, §11.1 delete table, §22 E7): the (column, value) an entity's delete
# sets, or None for a tombstone (tasks). The one map; FakeLedger and PG both import it.
# live_ids() excludes a row only when it is tombstoned or already in this end state, so a done or
# status-בוטל task stays included.
FORK_DELETE_END_STATE: Mapping[str, tuple[str, str] | None] = MappingProxyType({
    "leads": ("status", "לא רלוונטי"),
    "clients": ("status", "לא פעיל"),
    "works": ("status", "בוטלה"),
    "payments": ("kind", "בוטל"),
    "time": ("status", "בוטל"),
    "expenses": ("payment_status", "בוטל"),
    "tasks": None,
})

_CANCELLED: Mapping[str, tuple[str, str]] = MappingProxyType({
    "payments": ("kind", "בוטל"),
    "works": ("status", "בוטלה"),
    "time": ("status", "בוטל"),
})
_LIVE_TASK_STATUSES = frozenset({"לביצוע", "בתהליך", "ממתין"})


def is_live(entity: Entity, record: RecordDict, *, deleted: bool) -> bool:
    """False when tombstoned or in its cancellation status; a task is live only while open."""
    if deleted:
        return False
    if entity == "tasks":
        return record.get("status") in _LIVE_TASK_STATUSES
    cancelled = _CANCELLED.get(entity)
    return cancelled is None or record.get(cancelled[0]) != cancelled[1]


# ---------------------------------------------------------------------------------------------
# Lazy entry points into the V2-16 implementation. Importing this module never needs V2-16.


def _implementation(module: str, name: str) -> Callable[..., Any]:
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError):
        _LOG.warning("os_ledger op=load outcome=refused reason=implementation_missing")
        raise LedgerError("ledger_unavailable", reason="implementation_missing") from None


def core_authority_state(settings: Any) -> AuthorityState | None:
    """Context-free read of os_authority(core) on the runtime DSN, for V2-17's Sheets fence ONLY.

    None when no ledger DSN is configured, or when no os_workspaces row exists (the Sheets call
    proceeds). A DB error, or a missing V2-16 implementation while the DSN is set, is
    ``ledger_unavailable``.
    """
    if not getattr(settings, "owner_os_ledger_database_url", ""):
        return None
    core_state = _implementation("app.services.os_ledger.authority", "core_state")
    try:
        return core_state(settings)
    except LedgerError:
        raise
    except Exception:
        _LOG.warning("os_ledger op=authority_state outcome=refused reason=read_failed")
        raise LedgerError("ledger_unavailable", reason="read_failed") from None


def build_ledger(settings: Any) -> Ledger:
    """V2-16's ``os_ledger/core.build_ledger``; ``ledger_unavailable`` when off or not built yet."""
    return _implementation("app.services.os_ledger.core", "build_ledger")(settings)
