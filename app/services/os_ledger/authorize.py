"""Production trusted-context binding for the ledger (V2-19; V2_PG_CONTRACTS.md §9, I4-I6, I12).

This is the only app module that calls ``api._issue`` (pinned by
``tests/unit/test_trusted_context_ast.py``). Each constructor turns one already-authenticated
channel credential into a ``TrustedOwnerContext`` for one call:

* ``from_owner_read(principal)``: OS, from a cookie-session ``OwnerSessionPrincipal``; never a
  verified browser mutation.
* ``from_owner_mutation(principal)``: OS, only for a principal carrying
  ``owner_sessions._MUTATION_PROOF``, which ``owner_mutation_scope`` attaches after the exact
  Origin, the session and CSRF pass.
* ``from_telegram_decision(approval)``: TELEGRAM_APPROVAL, only for a persisted ApprovalRow whose
  decision was written by the compare-and-set (V2-28 commits the CAS before it calls this).
* ``from_mia_tool(principal)``: MIA, from the Telegram owner ``Principal`` of the tool loop.
* ``from_assistant(credential)``: ASSISTANT_CLAUDE / ASSISTANT_GPT, from a V2-37 credential.
* ``for_worker(job)``: WORKER, for one job; the owner is the single allow-listed id.

Every constructor re-checks the numeric owner allow-list (a username never grants, I6), takes the
ledger workspace and epoch from settings (I12), and sets ``auth_expires_at`` from the session or
credential expiry, or a short per-call bound. ``require(ctx, op)`` then enforces the §9 channel x
operation matrix and re-validates the context itself. The ledger still owns the kill switch, the
freeze, the backend and the authority state (§5.3): a context is authorization, not admission.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from types import MappingProxyType
from typing import Any, get_args

from sqlalchemy import inspect as orm_inspect
from sqlalchemy.exc import NoInspectionAvailable

from app.api import owner_sessions
from app.api.owner_sessions import OwnerSessionPrincipal
from app.capabilities.types import Principal
from app.db.models import ApprovalRow
from app.domain.approvals import DECISION_APPROVED, DECISION_REJECTED
from app.services.os_ledger import api
from app.services.owner_actions import ACTION_OWNER_EXTERNAL_WRITE, RESOURCE_OWNER_PROPOSAL

READ_SCOPE = "assaf-os:read"
PROPOSE_SCOPE = "assaf-os:propose"
APPROVE_SCOPE = "assaf-os:approve"
DIRECT_SCOPE = "assaf-os:direct"
OS_SCOPES = owner_sessions.ASSAF_OS_READ_SCOPES | owner_sessions.ASSAF_OS_WRITE_SCOPES
# §4 os_assistant_credentials.scopes is CHECK-fixed to exactly these three.
ASSISTANT_SCOPES = frozenset({"merchant_rules:read", "own_proposals:read", "statement:propose"})
ASSISTANT_CHANNELS: Mapping[str, str] = MappingProxyType(
    {"claude": "ASSISTANT_CLAUDE", "gpt": "ASSISTANT_GPT"}
)
WORKER_JOBS = frozenset({"import", "mirror", "verifier", "authority", "retention"})
# MIA, TELEGRAM_APPROVAL and WORKER contexts have no session behind them: each is minted for one
# call and expires after this bound (a Telegram one never outlives its approval either).
CONTEXT_TTL = timedelta(seconds=120)

CHANNELS: frozenset[str] = frozenset(get_args(api.Channel))
OPERATIONS: frozenset[str] = frozenset(get_args(api.Operation))
_DENY = "channel_operation_denied"
_OWNER_ID = re.compile(r"[1-9][0-9]{0,18}")  # ASCII, canonical: str(int(x)) == x
_WORKSPACE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
_CREDENTIAL_ID = re.compile(r"AC_[0-9a-f]{32}")
_LOG = logging.getLogger("mia.os_authorize")


# ---------------------------------------------------------------------------------------------
# The §9 matrix. A cell is ("scope", scope, needs_mutation), ("job", jobs), ("credential", scope)
# or ("deny", code). Columns: OS, MIA, TELEGRAM_APPROVAL, ASSISTANT_* (both), WORKER.


def _scope(scope: str, *, mutation: bool = False) -> tuple[Any, ...]:
    return ("scope", scope, mutation)


def _deny(code: str = _DENY) -> tuple[Any, ...]:
    return ("deny", code)


def _jobs(*jobs: str) -> tuple[Any, ...]:
    return ("job", frozenset(jobs))


def _credential(scope: str) -> tuple[Any, ...]:
    return ("credential", scope)


_ALLOW = ("allow",)
_COLUMNS = ("OS", "MIA", "TELEGRAM_APPROVAL", "ASSISTANT", "WORKER")
_D = _deny()
_ROWS: dict[str, tuple[tuple[Any, ...], ...]] = {
    # MIA reads are bounded (16k chars) by the model view (V2-20), not here.
    "read": (_scope(READ_SCOPE), _ALLOW, _D, _D, _jobs("verifier", "mirror")),
    "read_rules": (_scope(READ_SCOPE), _D, _D, _credential("merchant_rules:read"), _D),
    "read_own_batches": (_scope(READ_SCOPE), _D, _D, _credential("own_proposals:read"), _D),
    "propose": (_scope(PROPOSE_SCOPE, mutation=True), _ALLOW, _D, _D, _D),
    "propose_statement_batch": (_scope(PROPOSE_SCOPE, mutation=True), _D, _D,
                                _credential("statement:propose"), _D),
    "decide": (_scope(APPROVE_SCOPE, mutation=True), _deny("owner_approval_required"), _ALLOW,
               _D, _D),
    "decide_statement_batch": (_scope(APPROVE_SCOPE, mutation=True), _D, _D, _D, _D),
    "draft_statement_batch": (_scope(APPROVE_SCOPE, mutation=True), _D, _D, _D, _D),
    "commit_direct": (_scope(DIRECT_SCOPE, mutation=True), _D, _D, _D, _D),
    "undo": (_scope(DIRECT_SCOPE, mutation=True), _D, _D, _D, _D),
    "abandon": (_scope(DIRECT_SCOPE, mutation=True), _D, _D, _D, _D),
    # Issuing a key also needs the step-up token; V2-37 checks it.
    "manage_credentials": (_scope(APPROVE_SCOPE, mutation=True), _D, _D, _D, _D),
    "import_promote": (_D, _D, _D, _D, _jobs("import")),
    "authority_admin": (_D, _D, _D, _D, _jobs("authority")),
    "expire_batches": (_D, _D, _D, _D, _jobs("retention")),
}
MATRIX: Mapping[str, Mapping[str, tuple[Any, ...]]] = MappingProxyType({
    op: MappingProxyType(dict(zip(_COLUMNS, row, strict=True))) for op, row in _ROWS.items()
})
assert set(MATRIX) == OPERATIONS, "the matrix must cover every frozen api.Operation"


# ---------------------------------------------------------------------------------------------
# Shared checks


def _now() -> datetime:
    return datetime.now(UTC)


def _refuse(op: str, code: str, reason: str) -> api.LedgerError:
    # I7: the constructor and a reason code only, never an id or a credential.
    _LOG.warning("os_authorize op=%s outcome=%s reason=%s", op, code, reason)
    return api.LedgerError(code, reason=reason)


def _owner(value: Any, settings: Any, *, op: str, code: str = "owner_auth_required") -> int:
    """I6: the canonical ASCII numeric id, in the allow-list now. A username never grants."""
    if (type(value) is not str or _OWNER_ID.fullmatch(value) is None
            or value not in settings.telegram_owner_user_id_set()):
        raise _refuse(op, code, "owner_not_allowed")
    return int(value)


def _aware_future(value: Any) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None and value > _now()


def _binding(settings: Any, *, op: str) -> tuple[str, int]:
    """The ledger workspace and epoch (I12). Missing at use: ``ledger_unavailable`` (§3.3)."""
    workspace = getattr(settings, "assaf_os_workspace_id", "")
    if type(workspace) is not str or _WORKSPACE.fullmatch(workspace) is None:
        raise _refuse(op, "ledger_unavailable", "workspace_unconfigured")
    epoch = getattr(settings, "assaf_os_authority_epoch", 0)
    if type(epoch) is not int or epoch < 1:
        raise _refuse(op, "ledger_unavailable", "epoch_unset")
    return workspace, epoch


def _mint(channel: str, owner: int, settings: Any, *, op: str, scopes: frozenset[str],
          expires: datetime, verified: bool = False,
          credential_id: str | None = None) -> api.TrustedOwnerContext:
    workspace, epoch = _binding(settings, op=op)
    return api._issue(
        numeric_owner=owner, workspace=workspace, authority_epoch=epoch, channel=channel,
        scopes=frozenset(scopes), auth_expires_at=expires, browser_mutation_verified=verified,
        credential_id=credential_id,
    )


# ---------------------------------------------------------------------------------------------
# OS: the browser cookie session


def _session(principal: Any, settings: Any, *, op: str) -> tuple[int, frozenset[str], datetime]:
    if type(principal) is not OwnerSessionPrincipal:
        raise _refuse(op, "owner_auth_required", "principal_invalid")
    owner = _owner(principal.owner_id, settings, op=op)
    if not _aware_future(principal.expires_at):
        raise _refuse(op, "owner_auth_required", "session_expired")
    scopes = principal.scopes
    if not isinstance(scopes, frozenset) or not all(type(s) is str for s in scopes):
        raise _refuse(op, "owner_auth_required", "principal_invalid")
    return owner, scopes & OS_SCOPES, principal.expires_at


def from_owner_read(principal: OwnerSessionPrincipal, settings: Any) -> api.TrustedOwnerContext:
    """OS read context: ``browser_mutation_verified`` is always False."""
    op = "from_owner_read"
    owner, scopes, expires = _session(principal, settings, op=op)
    if READ_SCOPE not in scopes:
        raise _refuse(op, "scope_denied", "read_scope_missing")
    return _mint("OS", owner, settings, op=op, scopes=scopes, expires=expires, verified=False)


def from_owner_mutation(principal: OwnerSessionPrincipal,
                        settings: Any) -> api.TrustedOwnerContext:
    """OS write context, only behind ``owner_mutation_scope`` (exact Origin + session + CSRF)."""
    op = "from_owner_mutation"
    owner, scopes, expires = _session(principal, settings, op=op)
    if getattr(principal, "_mutation_proof", None) is not owner_sessions._MUTATION_PROOF:
        raise _refuse(op, "browser_mutation_verification_required", "mutation_proof_missing")
    if not scopes & owner_sessions.ASSAF_OS_WRITE_SCOPES:
        raise _refuse(op, "scope_denied", "write_scope_missing")
    return _mint("OS", owner, settings, op=op, scopes=scopes, expires=expires, verified=True)


# ---------------------------------------------------------------------------------------------
# TELEGRAM_APPROVAL: the owner's decision, after the ApprovalRow compare-and-set


def _parse_expiry(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def from_telegram_decision(approval: ApprovalRow, settings: Any) -> api.TrustedOwnerContext:
    """Only for a persisted owner card whose stored decision the CAS wrote.

    The row must be loaded from the database with no pending in-memory change, so a decision set
    on the object (or a row never stored) cannot mint a context. The caller commits the CAS first
    (V2-28 does, §5.3 Telegram); recovery after a crash reads the receipt, never a new decision.
    """
    op = "from_telegram_decision"
    if type(approval) is not ApprovalRow:
        raise _refuse(op, "owner_auth_required", "approval_invalid")
    try:
        state = orm_inspect(approval)
    except NoInspectionAvailable:
        raise _refuse(op, "owner_approval_required", "approval_not_stored") from None
    if not state.persistent or state.modified:
        raise _refuse(op, "owner_approval_required", "approval_not_stored")
    owner = _owner(approval.actor_id, settings, op=op)
    if (approval.channel != "telegram" or approval.action != ACTION_OWNER_EXTERNAL_WRITE
            or approval.resource_type != RESOURCE_OWNER_PROPOSAL):
        raise _refuse(op, "owner_approval_required", "approval_not_owner_card")
    decision = approval.decision
    if decision not in {DECISION_APPROVED, DECISION_REJECTED} or approval.approver != (
            approval.actor_id) or (decision == DECISION_APPROVED and not approval.approved_at):
        raise _refuse(op, "owner_approval_required", "approval_undecided")
    if str(approval.executed_at or "").strip():
        raise _refuse(op, "owner_approval_required", "approval_already_executed")
    expiry = _parse_expiry(approval.expires_at)
    if expiry is None or not _aware_future(expiry):
        raise _refuse(op, "owner_approval_required", "approval_expired")
    return _mint("TELEGRAM_APPROVAL", owner, settings, op=op, scopes=frozenset(),
                 expires=min(expiry, _now() + CONTEXT_TTL))


# ---------------------------------------------------------------------------------------------
# MIA: the owner tool loop


def from_mia_tool(principal: Principal, settings: Any) -> api.TrustedOwnerContext:
    """MIA context for one tool call: the numeric Telegram owner of the turn (§9 MIA column)."""
    op = "from_mia_tool"
    if type(principal) is not Principal or not principal.is_owner():
        raise _refuse(op, "owner_auth_required", "principal_not_owner")
    owner = _owner(principal.actor_id, settings, op=op)
    return _mint("MIA", owner, settings, op=op, scopes=frozenset(),
                 expires=_now() + CONTEXT_TTL)


# ---------------------------------------------------------------------------------------------
# ASSISTANT_*: a V2-37 propose-only credential (read by attribute; every refusal is uniform)


def from_assistant(credential: Any, settings: Any) -> api.TrustedOwnerContext:
    """ASSISTANT context. Any mismatch is the uniform 401 ``assistant_auth_required`` (§17.2)."""
    op = "from_assistant"
    workspace, epoch = _binding(settings, op=op)
    denied = "assistant_auth_required"
    credential_id = getattr(credential, "credential_id", None)
    kind = getattr(credential, "kind", None)
    channel = ASSISTANT_CHANNELS.get(kind) if type(kind) is str else None
    if (type(credential_id) is not str or _CREDENTIAL_ID.fullmatch(credential_id) is None
            or channel is None):
        raise _refuse(op, denied, "credential_invalid")
    owner = _owner(getattr(credential, "owner_id", None), settings, op=op, code=denied)
    if getattr(credential, "revoked_at", None) is not None:
        raise _refuse(op, denied, "credential_revoked")
    expires = getattr(credential, "expires_at", None)
    if not _aware_future(expires):
        raise _refuse(op, denied, "credential_expired")
    held_epoch = getattr(credential, "epoch", None)
    if (getattr(credential, "workspace_id", None) != workspace
            or type(held_epoch) is not int or held_epoch != epoch):
        raise _refuse(op, denied, "credential_binding_changed")  # I12: another epoch or workspace
    scopes = getattr(credential, "scopes", None)
    held = frozenset(scopes.split()) if isinstance(scopes, str) else scopes
    if held != ASSISTANT_SCOPES:
        raise _refuse(op, denied, "credential_scopes_invalid")
    return _mint(channel, owner, settings, op=op, scopes=ASSISTANT_SCOPES, expires=expires,
                 credential_id=credential_id)


# ---------------------------------------------------------------------------------------------
# WORKER: one privileged job, on behalf of the single owner


def for_worker(job: str, settings: Any) -> api.TrustedOwnerContext:
    """WORKER context for ``job``; the owner is ``min(telegram_owner_user_id_set())``, alone."""
    op = "for_worker"
    if type(job) is not str or job not in WORKER_JOBS:
        raise _refuse(op, "scope_denied", "worker_job_unknown")
    ids = settings.telegram_owner_user_id_set()
    if len(ids) != 1:
        raise _refuse(op, "ledger_unavailable", "worker_owner_not_single")
    owner = _owner(min(ids), settings, op=op, code="ledger_unavailable")
    return _mint("WORKER", owner, settings, op=op, scopes=frozenset({job}),
                 expires=_now() + CONTEXT_TTL)


# ---------------------------------------------------------------------------------------------
# require(ctx, op)


def _column(channel: str) -> str:
    return "ASSISTANT" if channel.startswith("ASSISTANT_") else channel


def _validate(ctx: Any) -> None:
    """The context must be one this module minted, and still well-formed and unexpired."""
    if type(ctx) is not api.TrustedOwnerContext or getattr(
            ctx, "_capability", None) is not api._CAPABILITY:
        raise api.LedgerError("invalid_trusted_context", reason="context_not_issued")
    channel = ctx.channel
    scopes = ctx.scopes
    well_formed = (
        type(channel) is str and channel in CHANNELS
        and type(ctx.numeric_owner) is int and ctx.numeric_owner > 0
        and type(ctx.workspace) is str and _WORKSPACE.fullmatch(ctx.workspace) is not None
        and type(ctx.authority_epoch) is int and ctx.authority_epoch >= 1
        and type(scopes) is frozenset and all(type(s) is str for s in scopes)
        and type(ctx.browser_mutation_verified) is bool
        and isinstance(ctx.auth_expires_at, datetime)
        and ctx.auth_expires_at.utcoffset() is not None
    )
    if well_formed:
        assistant = channel.startswith("ASSISTANT_")
        credential = ctx.credential_id
        well_formed = (
            # Only a cookie session behind owner_mutation_scope can carry a verified mutation.
            (channel == "OS" or ctx.browser_mutation_verified is False)
            and (type(credential) is str and _CREDENTIAL_ID.fullmatch(credential) is not None
                 if assistant else credential is None)
            and (channel != "WORKER" or (len(scopes) == 1 and scopes <= WORKER_JOBS))
        )
    if not well_formed:
        raise api.LedgerError("invalid_trusted_context", reason="context_malformed")
    if ctx.auth_expires_at <= _now():
        code = ("assistant_auth_required" if channel.startswith("ASSISTANT_")
                else "owner_auth_required")
        raise api.LedgerError(code, reason="context_expired")


def require(ctx: api.TrustedOwnerContext, op: str) -> None:
    """Enforce the §9 matrix for ``op``; a forbidden cell raises its listed ``LedgerError``."""
    if op not in MATRIX:
        raise ValueError("unknown ledger operation")
    _validate(ctx)
    cell = MATRIX[op][_column(ctx.channel)]
    kind = cell[0]
    if kind == "deny":
        raise api.LedgerError(cell[1], reason=f"{_column(ctx.channel).lower()}_{op}")
    if kind == "scope":
        if cell[1] not in ctx.scopes:
            raise api.LedgerError("scope_denied", reason="scope_missing")
        if cell[2] and ctx.browser_mutation_verified is not True:
            raise api.LedgerError("browser_mutation_verification_required",
                                  reason="mutation_unverified")
    elif kind == "job":
        if not ctx.scopes & cell[1]:
            raise api.LedgerError("scope_denied", reason="worker_job_denied")
    elif kind == "credential":
        if cell[1] not in ctx.scopes:
            raise api.LedgerError("scope_denied", reason="credential_scope_missing")
