"""Assaf OS v2 statement routes (V2-26; contract §12.3, §12.4, §9, §10.5).

Under ``/v1/owner/os/unified``: ``GET /statements``, ``GET /statements/{statement_id}``,
``POST /statement-batches`` (the owner's paste, source ``ASSISTANT_PASTE``),
``GET /statement-batches/{id}`` and ``/lines``, ``POST /statement-batches/{id}/draft``, ``/approve``
and ``/reject``, and ``GET /merchant-rules``.

Every route is gated first: unless ``assaf_os_statements_enabled`` is on and ``assaf_os_backend`` is
``postgres`` it is a 404 ``operation_unavailable``, and any ``Authorization`` header is refused
(I4). A write then needs the owner's cookie session, the exact Origin and CSRF
(``owner_mutation_scope``); its trusted context comes only from V2-19's ``authorize`` constructors
(``from_owner_mutation`` / ``from_owner_read``), resolved at call time. The service then enforces
the §9 matrix itself (``authorize.require``), so this module only admits, delegates and renders.
If that module cannot be imported, every route fails closed with ``ledger_unavailable``.

Bodies are capped by ``OwnerOSBoundary`` (128 KiB for the paste, 16 KiB for the rest, §10.5).
"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api import owner_sessions
from app.api.owner_sessions import (
    CSRF_HEADER,
    OwnerSessionPrincipal,
    get_owner_session_db,
    owner_scope,
)
from app.core.config import Settings, get_settings
from app.services.os_ledger import api
from app.services.os_ledger.statement_wire import batch_created, receipt_v2
from app.services.os_ledger.statements import (
    PASTE,
    LedgerStatementService,
    build_statement_service,
)

ROOT = "/v1/owner/os/unified"
AUTHORIZE_MODULE = "app.services.os_ledger.authorize"  # V2-19; imported at call time only
READ_SCOPE = "assaf-os:read"
PROPOSE_SCOPE = "assaf-os:propose"
APPROVE_SCOPE = "assaf-os:approve"
_LOG = logging.getLogger("mia.os_v2")
_HEADERS = {"Cache-Control": "private, no-store"}

router = APIRouter()


def service_for(settings: Settings) -> LedgerStatementService:
    """The service on the PostgreSQL ledger. Tests replace this one function."""
    return build_statement_service(settings)


def route_guard(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Runs before authentication, so a disabled feature is invisible (404)."""
    if settings.assaf_os_statements_enabled is not True or settings.assaf_os_backend != "postgres":
        raise api.LedgerError("operation_unavailable", reason="statements_off")
    if "authorization" in request.headers:
        raise api.LedgerError("owner_auth_required", reason="authorization_header")


def _mutation(scope: str) -> Callable[..., OwnerSessionPrincipal]:
    """Cookie session + exact Origin + CSRF with ``scope``, through ``owner_mutation_scope`` (the
    one place V2-19 sets the mutation proof)."""

    def dependency(
        request: Request,
        csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
        db: Session = Depends(get_owner_session_db),
        settings: Settings = Depends(get_settings),
    ) -> OwnerSessionPrincipal:
        guard = owner_sessions.owner_mutation_scope(scope)
        return guard(request, csrf_token=csrf_token, login_proof=None, db=db, settings=settings)

    return dependency


propose_principal = _mutation(PROPOSE_SCOPE)
approve_principal = _mutation(APPROVE_SCOPE)
_read_scope = owner_scope(READ_SCOPE)


def read_principal(
    principal: OwnerSessionPrincipal = Depends(_read_scope),
) -> OwnerSessionPrincipal:
    return principal


def _authorize() -> Any:
    try:
        return importlib.import_module(AUTHORIZE_MODULE)
    except ImportError:
        _LOG.warning("os_v2 op=admit outcome=refused reason=authorize_missing")
        raise api.LedgerError("ledger_unavailable", reason="authorize_missing") from None


def _context(principal: OwnerSessionPrincipal, settings: Settings, *,
             read: bool) -> api.TrustedOwnerContext:
    """The trusted context, from V2-19 only (§9): ``from_owner_read`` (never a verified mutation)
    for a read, ``from_owner_mutation`` (the proof set by ``owner_mutation_scope``) for a write."""
    module = _authorize()
    try:
        build = module.from_owner_read if read else module.from_owner_mutation
    except AttributeError:
        raise api.LedgerError("ledger_unavailable", reason="authorize_incomplete") from None
    return build(principal, settings)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise ValueError("duplicate_key")
        found[key] = value
    return found


def _bad_body(reason: str) -> api.LedgerError:
    return api.LedgerError("validation_error", details={"reason": reason}, reason=reason)


async def _object(request: Request, required: set[str],
                  optional: frozenset[str] = frozenset()) -> dict[str, Any]:
    """A JSON object with every required key and no key outside the optional ones."""
    raw = await request.body()
    try:
        body = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError):
        raise _bad_body("invalid_json") from None
    if not isinstance(body, dict) or not required <= set(body) or not set(body) <= (
            required | optional):
        raise _bad_body("invalid_body")
    return body


def _lines_query(query: str) -> tuple[str | None, int]:
    """``after`` (an opaque cursor) and ``limit``; any other key or a duplicate is 400."""
    pairs = parse_qsl(query, keep_blank_values=True)
    names = [name for name, _ in pairs]
    if len(set(names)) != len(names) or not set(names) <= {"after", "limit"}:
        raise api.LedgerError("invalid_query", reason="query_keys")
    found = dict(pairs)
    after = found.get("after")
    if after is not None and not 0 < len(after) <= 512:
        raise api.LedgerError("invalid_query", reason="after")
    limit = found.get("limit", "100")
    if not (limit.isascii() and limit.isdigit() and len(limit) <= 4):
        raise api.LedgerError("invalid_query", reason="limit")
    return after, int(limit)


def _text(value: Any) -> str:
    if type(value) is not str:
        raise api.LedgerError("invalid_request_id", reason="request_id_type")
    return value


async def _respond(run: Callable[[], dict[str, Any]]) -> JSONResponse:
    # The ledger is synchronous (a DB transaction), so it never runs on the event loop.
    return JSONResponse(await run_in_threadpool(run), status_code=200, headers=_HEADERS)


@router.get(ROOT + "/statements", dependencies=[Depends(route_guard)])
async def statement_list(
    principal: OwnerSessionPrincipal = Depends(read_principal),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    def run() -> dict[str, Any]:
        ctx = _context(principal, settings, read=True)
        return service_for(settings).cycles(ctx)

    return await _respond(run)


@router.get(ROOT + "/statements/{statement_id}", dependencies=[Depends(route_guard)])
async def statement_view(
    statement_id: str,
    principal: OwnerSessionPrincipal = Depends(read_principal),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    def run() -> dict[str, Any]:
        ctx = _context(principal, settings, read=True)
        return service_for(settings).statement_view(ctx, statement_id)

    return await _respond(run)


@router.post(ROOT + "/statement-batches", dependencies=[Depends(route_guard)])
async def submit_batch(
    request: Request,
    principal: OwnerSessionPrincipal = Depends(propose_principal),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    raw = await request.body()

    def run() -> dict[str, Any]:
        ctx = _context(principal, settings, read=False)
        return batch_created(ctx, service_for(settings).submit(ctx, raw, source=PASTE))

    return await _respond(run)


@router.get(ROOT + "/statement-batches/{proposal_id}", dependencies=[Depends(route_guard)])
async def batch_review(
    proposal_id: str,
    principal: OwnerSessionPrincipal = Depends(read_principal),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    def run() -> dict[str, Any]:
        ctx = _context(principal, settings, read=True)
        return service_for(settings).review(ctx, proposal_id)

    return await _respond(run)


@router.get(ROOT + "/statement-batches/{proposal_id}/lines",
            dependencies=[Depends(route_guard)])
async def batch_lines(
    proposal_id: str,
    request: Request,
    principal: OwnerSessionPrincipal = Depends(read_principal),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    query = request.url.query

    def run() -> dict[str, Any]:
        after, limit = _lines_query(query)
        ctx = _context(principal, settings, read=True)
        return service_for(settings).lines(ctx, proposal_id, after=after, limit=limit)

    return await _respond(run)


@router.post(ROOT + "/statement-batches/{proposal_id}/draft",
             dependencies=[Depends(route_guard)])
async def batch_draft(
    proposal_id: str,
    request: Request,
    principal: OwnerSessionPrincipal = Depends(approve_principal),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    body = await _object(request, {"draft_revision", "overrides"}, frozenset({"choices"}))

    def run() -> dict[str, Any]:
        ctx = _context(principal, settings, read=False)
        return service_for(settings).save_draft(
            ctx, proposal_id, body["draft_revision"], body["overrides"], body.get("choices", {}))

    return await _respond(run)


@router.post(ROOT + "/statement-batches/{proposal_id}/approve",
             dependencies=[Depends(route_guard)])
async def batch_approve(
    proposal_id: str,
    request: Request,
    principal: OwnerSessionPrincipal = Depends(approve_principal),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    body = await _object(request, {"request_id", "review_digest"})

    def run() -> dict[str, Any]:
        ctx = _context(principal, settings, read=False)
        result = service_for(settings).approve(
            ctx, proposal_id, _text(body["request_id"]), body["review_digest"])
        return receipt_v2(ctx, result, datetime.now(UTC))

    return await _respond(run)


@router.post(ROOT + "/statement-batches/{proposal_id}/reject",
             dependencies=[Depends(route_guard)])
async def batch_reject(
    proposal_id: str,
    request: Request,
    principal: OwnerSessionPrincipal = Depends(approve_principal),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    body = await _object(request, {"request_id"})

    def run() -> dict[str, Any]:
        ctx = _context(principal, settings, read=False)
        result = service_for(settings).reject(ctx, proposal_id, _text(body["request_id"]))
        return receipt_v2(ctx, result, datetime.now(UTC))

    return await _respond(run)


@router.get(ROOT + "/merchant-rules", dependencies=[Depends(route_guard)])
async def merchant_rules(
    principal: OwnerSessionPrincipal = Depends(read_principal),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    def run() -> dict[str, Any]:
        ctx = _context(principal, settings, read=True)
        return service_for(settings).merchant_rules(ctx)

    return await _respond(run)
