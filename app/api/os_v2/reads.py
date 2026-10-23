"""Assaf OS v2 reads served by V2-19 (V2_PG_CONTRACTS.md §9, §10.1, §10.2, §22 E12/E19).

``GET /pending`` and ``GET /proposal-requests/{uuid}`` under ``/v1/owner/os/unified``. This router
is mounted from ``app/main.py`` (E19), so the backend is the gate: unless ``assaf_os_backend`` is
``postgres`` both paths answer 404 ``operation_unavailable`` before anything else, and
``/proposal-requests`` stays absent exactly as on v1. Any ``Authorization`` header is then 401
(I4); the owner's cookie session with ``assaf-os:read`` and the exact Origin follow
(``owner_scope``). The trusted context comes only from ``authorize.from_owner_read`` and
``require(ctx, "read")``.

The bodies are V2-20's serializer (``contract.py``), which is not on this base: the small private
helpers below follow §10.1, §10.2 and E12 exactly, and fail closed (I9) with
``ledger_integrity_failed`` on any ledger answer outside the binding or the §10.2 cross-check.
Reads never write, never log a body, and run off the event loop.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.owner_sessions import OwnerSessionPrincipal, owner_scope
from app.core.config import Settings, get_settings
from app.prototypes.assaf_os_domain import SCHEMA
from app.services.os_ledger import api, authorize

ROOT = "/v1/owner/os/unified"
API_VERSION = "assaf-os.v2"
SOURCE = "postgresql"
APPROVAL_GUARD_VERSION = 1
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_AFTER_CHARS = 512
_PENDING_KEYS = frozenset({"after", "limit"})
_LIMIT = re.compile(r"[1-9][0-9]{0,2}")
_TOKEN = re.compile(r"[\x21-\x7e]{1,512}")
# §5.3 receipt keys: which decision id a terminal status is stored under.
_APPROVAL_KEYED = frozenset({"committed", "target_changed", "abandoned"})
_REJECTION_KEYED = frozenset({"rejected", "superseded"})
_RECORD_KINDS = frozenset({"proposal", "owner_direct", "owner_undo"})
# The input-role fields per entity, from the frozen schema (§10.2: identical to v1).
_INPUT_FIELDS: Mapping[str, frozenset[str]] = {
    entity: frozenset(f["key"] for f in fields if f["role"] == "input")
    for entity, fields in SCHEMA["entities"].items()
}
_HEADERS = {"Cache-Control": "private, no-store"}
_LOG = logging.getLogger("mia.os_v2")

router = APIRouter()


def route_guard(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Runs before authentication: off the postgres backend the routes do not exist (404)."""
    if settings.assaf_os_backend != "postgres":
        raise api.LedgerError("operation_unavailable", reason="backend_not_postgres")
    if "authorization" in request.headers:
        raise api.LedgerError("owner_auth_required", reason="authorization_header")


_read_scope = owner_scope(authorize.READ_SCOPE)


# ---------------------------------------------------------------------------------------------
# Wire helpers (§10.1 binding, §10.2 proposal_v2 / receipt_v2 / request lookup, E12)


def _integrity(reason: str) -> api.LedgerError:
    return api.LedgerError("ledger_integrity_failed", reason=reason)


def _iso(value: Any) -> str:
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise _integrity("naive_datetime")
    return value.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _binding(ctx: api.TrustedOwnerContext, observed_version: int | None) -> dict[str, Any]:
    body: dict[str, Any] = {"api_version": API_VERSION, "source": SOURCE,
                            "workspace_id": ctx.workspace, "authority_epoch": ctx.authority_epoch}
    if observed_version is not None:
        if type(observed_version) is not int or observed_version < 0:
            raise _integrity("observed_version_invalid")
        body["observed_version"] = observed_version
    return body


def _changed_input(entity: str, before: Mapping[str, Any] | None,
                   after: Mapping[str, Any]) -> dict[str, Any]:
    keys = _INPUT_FIELDS.get(entity, frozenset())
    return {key: after[key] for key in after
            if key in keys and (before is None or before.get(key) != after.get(key))}


def _states(row: api.ProposalRow) -> tuple[dict[str, Any], Any, Any]:
    """``input``, ``before`` and ``after``, derived from the proposal's immutable mutations."""
    mutations = row.mutations
    if row.kind == "proposal" and row.entity == "promote_lead":
        by_entity = {m.entity: m for m in mutations}
        if len(mutations) != 2 or set(by_entity) != {"leads", "clients"}:
            raise _integrity("promotion_mutations")
        lead, client = by_entity["leads"], by_entity["clients"]
        before = {"lead": dict(lead.before) if lead.before else None,
                  "client": dict(client.before) if client.before else None}
        return {}, before, {"lead": dict(lead.after), "client": dict(client.after)}
    if row.kind in _RECORD_KINDS and len(mutations) == 1:
        mutation = mutations[0]
        before = dict(mutation.before) if mutation.before else None
        after = dict(mutation.after)
        return _changed_input(mutation.entity, before, after), before, after
    if row.kind == "proposal":
        raise _integrity("proposal_mutations")
    return {}, None, None  # a bulk, an abandon marker, a statement batch or an import


def _proposal_v2(ctx: api.TrustedOwnerContext, row: api.ProposalRow) -> dict[str, Any]:
    if row.workspace_id != ctx.workspace or row.epoch != ctx.authority_epoch:
        raise _integrity("proposal_binding")  # I12: never another workspace or epoch
    input_, before, after = _states(row)
    return _binding(ctx, None) | {
        "status": row.status,
        "proposal_id": row.proposal_id,
        "request_id": row.request_id,
        "approval_request_id": row.approval_id,
        "rejection_request_id": row.rejection_id,
        "approval_guard_version": APPROVAL_GUARD_VERSION,
        "actor_source": row.source,
        "kind": row.kind,
        "expires_at": _iso(row.expires_at),
        "entity": row.entity,
        "record_id": row.record_id,
        "expected_revision": row.expected_revision,
        "input": input_,
        "before": before,
        "after": after,
    }


def _receipt_v2(ctx: api.TrustedOwnerContext, result: api.ReceiptResult,
                server_now: datetime) -> dict[str, Any]:
    """``receipt_v2`` for a found receipt (a lookup only asks for one the ledger listed)."""
    receipt, proposal = result.receipt, result.proposal
    if not result.found or receipt is None or proposal is None:
        raise _integrity("decision_without_receipt")
    if receipt.workspace_id != ctx.workspace or receipt.epoch != ctx.authority_epoch:
        raise _integrity("receipt_binding")
    undo = None
    if receipt.kind == "owner_direct":
        view = result.undo
        if view is None or receipt.status != "committed":
            undo = {"state": "none", "until": None, "undo_request_id": None}
        else:
            undo = {"state": view.state, "until": _iso(view.until) if view.until else None,
                    "undo_request_id": view.undo_request_id}
    shows_records = receipt.status in {"committed", "target_changed"}
    return _binding(ctx, result.observed_version) | {
        "status": receipt.status,
        "request_id": receipt.request_id,
        "proposal_id": receipt.proposal_id,
        "kind": receipt.kind,
        "actor_source": proposal.source,
        "commit_version": receipt.commit_version,
        "committed_at": _iso(receipt.completed_at) if receipt.status == "committed" else None,
        "server_now": _iso(server_now),
        "records": ([{"entity": entity, "record": dict(record)}
                     for entity, record in result.records] if shows_records else []),
        "undo": undo,
        "undo_of": receipt.undo_of,
        "reason_codes": list(receipt.reason_codes),
    }


def _cross_check(proposal: Mapping[str, Any], receipt: Mapping[str, Any] | None) -> None:
    """§10.2: derived from the §5.3 receipt-key table. With no receipt only ``pending`` holds."""
    status = proposal["status"]
    if receipt is None:
        if status != "pending":
            raise _integrity("decision_without_receipt")
        return
    if (receipt["status"] != status or receipt["proposal_id"] != proposal["proposal_id"]
            or receipt["actor_source"] != proposal["actor_source"]):
        raise _integrity("proposal_request_cross_check")
    approval, rejection = proposal["approval_request_id"], proposal["rejection_request_id"]
    allowed = ({approval} if status in _APPROVAL_KEYED
               else {rejection} if status in _REJECTION_KEYED
               else {approval, rejection} if status == "expired" else set())
    if receipt["request_id"] not in allowed:
        raise _integrity("proposal_request_cross_check")


# ---------------------------------------------------------------------------------------------
# Query parsing (§10.2: an exact allow-list; anything else is 400 invalid_query)


def _invalid_query() -> api.LedgerError:
    return api.LedgerError("invalid_query", reason="invalid_query")


def _pending_query(raw: str) -> tuple[str | None, int]:
    if not raw:
        return None, DEFAULT_LIMIT
    try:
        pairs = parse_qsl(raw, keep_blank_values=True, strict_parsing=True,
                          errors="strict", max_num_fields=len(_PENDING_KEYS))
    except (ValueError, UnicodeDecodeError):
        raise _invalid_query() from None
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)) or not set(keys) <= _PENDING_KEYS:
        raise _invalid_query()
    values = dict(pairs)
    after = values.get("after")
    if after is not None and (len(after) > MAX_AFTER_CHARS or not _TOKEN.fullmatch(after)):
        raise _invalid_query()
    limit = DEFAULT_LIMIT
    if "limit" in values:
        if not _LIMIT.fullmatch(values["limit"]) or int(values["limit"]) > MAX_LIMIT:
            raise _invalid_query()
        limit = int(values["limit"])
    return after, limit


# ---------------------------------------------------------------------------------------------
# Routes


def _context(principal: OwnerSessionPrincipal, settings: Settings) -> api.TrustedOwnerContext:
    ctx = authorize.from_owner_read(principal, settings)
    authorize.require(ctx, "read")
    return ctx


async def _respond(op: str, run: Callable[[], dict[str, Any]]) -> JSONResponse:
    started = time.perf_counter()
    try:
        # The ledger is synchronous (a DB read transaction): never on the event loop.
        body = await run_in_threadpool(run)
    except api.LedgerError as exc:
        _LOG.info("os_v2_read op=%s outcome=%s ms=%d", op, exc.code,
                  int((time.perf_counter() - started) * 1000))
        raise
    _LOG.info("os_v2_read op=%s outcome=ok ms=%d", op, int((time.perf_counter() - started) * 1000))
    return JSONResponse(body, status_code=200, headers=_HEADERS)


@router.get(ROOT + "/pending", dependencies=[Depends(route_guard)])
async def pending(
    request: Request,
    principal: OwnerSessionPrincipal = Depends(_read_scope),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Pending ``proposal``-kind items not past ``expires_at`` (§10.2); batches are elsewhere."""
    after, limit = _pending_query(request.url.query)

    def run() -> dict[str, Any]:
        ctx = _context(principal, settings)
        page = api.build_ledger(settings).pending(ctx, after=after, limit=limit)
        if len(page.items) > limit:
            raise _integrity("pending_over_limit")
        items = []
        for row in page.items:
            if row.kind != "proposal" or row.status != "pending":
                raise _integrity("pending_item_not_pending")
            items.append(_proposal_v2(ctx, row))
        return _binding(ctx, page.observed_version) | {"items": items,
                                                       "next_cursor": page.next_cursor}

    return await _respond("pending", run)


@router.get(ROOT + "/proposal-requests/{request_id}", dependencies=[Depends(route_guard)])
async def proposal_request(
    request_id: str,
    request: Request,
    principal: OwnerSessionPrincipal = Depends(_read_scope),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    """Recover a lost creation acknowledgement by reading (I1): never by re-posting."""
    if request.url.query:
        raise _invalid_query()
    request_id = api.canonical_uuid(request_id)

    def run() -> dict[str, Any]:
        ctx = _context(principal, settings)
        ledger = api.build_ledger(settings)
        found = ledger.creation_request(ctx, request_id)
        proposal = receipt = None
        if found.found:
            row = found.proposal
            if row is None or row.request_id != request_id:
                raise _integrity("creation_request_mismatch")
            proposal = _proposal_v2(ctx, row)
            if found.decision is not None:
                answer = ledger.receipt(ctx, found.decision.request_id)
                receipt = _receipt_v2(ctx, answer, datetime.now(UTC))
            _cross_check(proposal, receipt)
        elif found.proposal is not None or found.decision is not None:
            raise _integrity("creation_request_mismatch")
        return _binding(ctx, found.observed_version) | {
            "request_id": request_id,
            "status": "found" if proposal is not None else "unknown",
            "proposal": proposal,
            "receipt": receipt,
            "reason_codes": [] if proposal is not None else ["proposal_request_unknown"],
        }

    return await _respond("proposal_request", run)
