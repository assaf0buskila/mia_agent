"""Assaf OS v2 owner-direct routes (V2-24; contract §11.1, §9, §10.2, §10.5).

``POST /commits``, ``POST /commits/{request_id}/undo``, ``POST /commits/{request_id}/abandon`` and
``GET /deleted``, under ``/v1/owner/os/unified``. Every route is gated first: unless
``assaf_os_owner_direct_enabled`` is on and ``assaf_os_backend`` is ``postgres`` it is a 404
``operation_unavailable``, and any ``Authorization`` header is refused (I4). A write then needs the
owner's cookie session, the exact Origin and CSRF with ``assaf-os:direct``
(``owner_mutation_scope``); its trusted context comes only from V2-19's
``authorize.from_owner_mutation`` (``from_owner_read`` for the read) and ``require``. V2-19 is on
master, so both are real; the module is still resolved at call time and, if the scope or the module
were ever missing, every route would fail closed with ``ledger_unavailable`` and write nothing.

Bodies are capped by ``OwnerOSBoundary`` (16 KiB for these routes, §10.5). The service decides the
source (``OS_DIRECT``) and every outcome; this module only admits, delegates and renders.
"""

from __future__ import annotations

import importlib
import json
import logging
from collections.abc import Callable
from typing import Annotated, Any

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
from app.core.owner_timing import owner_phase
from app.services.os_ledger import api, owner_direct

ROOT = "/v1/owner/os/unified"
AUTHORIZE_MODULE = "app.services.os_ledger.authorize"  # V2-19; resolved at call time (fail closed)
_LOG = logging.getLogger("mia.os_v2")
_HEADERS = {"Cache-Control": "private, no-store"}

router = APIRouter()


def route_guard(request: Request, settings: Settings = Depends(get_settings)) -> None:
    """Runs before authentication, so a disabled feature is invisible (404)."""
    if (settings.assaf_os_owner_direct_enabled is not True
            or settings.assaf_os_backend != "postgres"):
        raise api.LedgerError("operation_unavailable", reason="owner_direct_off")
    if "authorization" in request.headers:
        raise api.LedgerError("owner_auth_required", reason="authorization_header")


def direct_principal(
    request: Request,
    csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
    db: Session = Depends(get_owner_session_db),
    settings: Settings = Depends(get_settings),
) -> OwnerSessionPrincipal:
    """Cookie session + exact Origin + CSRF with ``assaf-os:direct``, through the one
    ``owner_mutation_scope`` (V2-19 sets the mutation proof there). Should the scope ever leave the
    mutation allow-list, ``owner_mutation_scope`` refuses it and so does this route."""
    try:
        dependency = owner_sessions.owner_mutation_scope(owner_direct.DIRECT_SCOPE)
    except ValueError:
        _LOG.warning("os_v2 op=admit outcome=refused reason=direct_scope_unknown")
        raise api.LedgerError("ledger_unavailable", reason="direct_scope_unknown") from None
    return dependency(request, csrf_token=csrf_token, login_proof=None, db=db, settings=settings)


_read_scope = owner_scope(owner_direct.READ_SCOPE)


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


def _context(principal: OwnerSessionPrincipal, settings: Settings, op: str, *,
             read: bool = False) -> api.TrustedOwnerContext:
    """The trusted context, from V2-19 only (§9): ``from_owner_mutation`` (OS) for a write,
    ``from_owner_read`` for the read, then ``require(ctx, op)``."""
    module = _authorize()
    try:
        build = module.from_owner_read if read else module.from_owner_mutation
        require = module.require
    except AttributeError:
        raise api.LedgerError("ledger_unavailable", reason="authorize_incomplete") from None
    ctx = build(principal, settings)
    require(ctx, op)
    return ctx


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


async def _body(request: Request) -> Any:
    """The JSON body (the boundary has already capped and checked it); empty means ``{}``."""
    raw = await request.body()
    if not raw:
        return {}
    try:
        return json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError):
        raise api.LedgerError("validation_error", details={"reason": "invalid_json"},
                              reason="invalid_json") from None


async def _respond(run: Callable[[], dict[str, Any]], *, stage: str) -> JSONResponse:
    def timed() -> dict[str, Any]:
        # Server-Timing "ledger": the ledger call, timed where it runs (silent outside a request).
        with owner_phase("ledger", stage):
            return run()

    # The ledger is synchronous (a DB transaction), so it never runs on the event loop.
    return JSONResponse(await run_in_threadpool(timed), status_code=200, headers=_HEADERS)


@router.post(ROOT + "/commits", dependencies=[Depends(route_guard)])
async def commit(
    request: Request,
    principal: OwnerSessionPrincipal = Depends(direct_principal),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    body = await _body(request)

    def run() -> dict[str, Any]:
        ctx = _context(principal, settings, "commit_direct")
        return owner_direct.commit(api.build_ledger(settings), ctx, body, settings=settings)

    return await _respond(run, stage="os_commit")


@router.post(ROOT + "/commits/{request_id}/undo", dependencies=[Depends(route_guard)])
async def undo(
    request_id: str,
    request: Request,
    principal: OwnerSessionPrincipal = Depends(direct_principal),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    body = await _body(request)

    def run() -> dict[str, Any]:
        ctx = _context(principal, settings, "undo")
        return owner_direct.undo(api.build_ledger(settings), ctx, request_id, body,
                                 settings=settings)

    return await _respond(run, stage="os_commit")


@router.post(ROOT + "/commits/{request_id}/abandon", dependencies=[Depends(route_guard)])
async def abandon(
    request_id: str,
    request: Request,
    principal: OwnerSessionPrincipal = Depends(direct_principal),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    body = await _body(request)

    def run() -> dict[str, Any]:
        ctx = _context(principal, settings, "abandon")
        return owner_direct.abandon(api.build_ledger(settings), ctx, request_id, body,
                                    settings=settings)

    return await _respond(run, stage="os_commit")


@router.get(ROOT + "/deleted", dependencies=[Depends(route_guard)])
async def deleted(
    request: Request,
    principal: OwnerSessionPrincipal = Depends(read_principal),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    query = request.url.query

    def run() -> dict[str, Any]:
        ctx = _context(principal, settings, "read", read=True)
        return owner_direct.deleted(api.build_ledger(settings), ctx, query, settings=settings)

    return await _respond(run, stage="os_read")
