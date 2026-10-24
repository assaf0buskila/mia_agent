"""Assaf OS v2 owner routes for the assistant keys (V2_PG_CONTRACTS.md §17.1, §9, §10.5; V2-37).

Under ``/v1/owner/os/unified`` (the module name is erratum E34's; the URL is unchanged):

* ``GET  /assistant-credentials``: the list, no secret;
* ``POST /assistant-credentials`` with ``{label, kind, expires_in_days?}``: issues a key. It needs a
  step-up token for THIS session in ``X-Step-Up`` (403 ``step_up_required`` otherwise) and
  returns the list item plus the secret, once;
* ``POST /assistant-credentials/{id}/revoke`` and ``POST /assistant-credentials/revoke-all``: return
  the list. Revoking a key rejects that key's pending batches in the same ledger transaction.

Every body carries the §10.1 binding and none carries ``observed_version`` (§22 E12 a, b).

The owner surface is cookie-session only: a request with any ``Authorization`` header is a 401 from
the session layer before anything else, and a write needs the exact Origin and CSRF
(``owner_mutation_scope``). The ledger context comes from V2-19's ``authorize`` constructors only,
and the §9 ``manage_credentials`` cell (approve scope plus a verified mutation) is enforced by the
service. A disabled assistant feature is a 404 ``operation_unavailable``, after authentication.
Bodies are capped by ``OwnerOSBoundary`` (16 KiB, §10.5).
"""

from __future__ import annotations

import json
import logging
import time
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
from app.services import assistant_keys as keys
from app.services import assistant_step_up as step_up
from app.services.os_ledger import api, authorize

ROOT = "/v1/owner/os/unified"
APPROVE_SCOPE = "assaf-os:approve"
STEP_UP_HEADER = "X-Step-Up"
HEADERS = {"Cache-Control": "private, no-store"}
_LOG = logging.getLogger("mia.os_v2")

router = APIRouter()


def service_for(settings: Settings) -> keys.AssistantCredentialService:
    """The owner's key service on the PostgreSQL ledger. Tests replace this one function."""
    return keys.AssistantCredentialService(api.build_ledger(settings), settings)


def gate(settings: Settings) -> None:
    """A disabled feature (or the sheets backend) is a 404, after the owner is authenticated."""
    if settings.assaf_os_assistant_enabled is not True or settings.assaf_os_backend != "postgres":
        raise api.LedgerError("operation_unavailable", reason="assistant_off")


def approve_mutation(
    request: Request,
    csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
    db: Session = Depends(get_owner_session_db),
    settings: Settings = Depends(get_settings),
) -> OwnerSessionPrincipal:
    """Cookie session + exact Origin + CSRF with ``assaf-os:approve``, through
    ``owner_mutation_scope`` (the one place V2-19 sets the mutation proof)."""
    guard = owner_sessions.owner_mutation_scope(APPROVE_SCOPE)
    return guard(request, csrf_token=csrf_token, login_proof=None, db=db, settings=settings)


def read_principal(
    principal: OwnerSessionPrincipal = Depends(owner_scope(APPROVE_SCOPE)),
) -> OwnerSessionPrincipal:
    return principal


def bad_body(reason: str) -> api.LedgerError:
    return api.LedgerError("validation_error", details={"reason": reason}, reason=reason)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for key, value in pairs:
        if key in found:
            raise ValueError("duplicate_key")
        found[key] = value
    return found


def parse_object(raw: bytes, required: set[str], optional: frozenset[str] = frozenset(),
                 ) -> dict[str, Any]:
    """A JSON object with every required key and no key outside the optional ones."""
    try:
        body = json.loads(raw, object_pairs_hook=_unique)
    except (ValueError, UnicodeError):
        raise bad_body("invalid_json") from None
    if not isinstance(body, dict) or not required <= set(body) or not set(body) <= (
            required | optional):
        raise bad_body("invalid_body")
    return body


def empty_object(raw: bytes) -> None:
    """An empty body, or ``{}``: revoke takes no input."""
    if raw.strip():
        parse_object(raw, set())


async def respond(run: Callable[[], dict[str, Any]]) -> JSONResponse:
    # The ledger is synchronous (a DB transaction), so it never runs on the event loop.
    return JSONResponse(await run_in_threadpool(run), status_code=200, headers=HEADERS)


@router.get(ROOT + "/assistant-credentials")
async def list_keys(
    principal: OwnerSessionPrincipal = Depends(read_principal),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    def run() -> dict[str, Any]:
        gate(settings)
        ctx = authorize.from_owner_read(principal, settings)
        return service_for(settings).list(ctx)

    return await respond(run)


@router.post(ROOT + "/assistant-credentials")
async def issue_key(
    request: Request,
    x_step_up: Annotated[str | None, Header(alias=STEP_UP_HEADER)] = None,
    principal: OwnerSessionPrincipal = Depends(approve_mutation),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    raw = await request.body()

    def run() -> dict[str, Any]:
        gate(settings)
        ctx = authorize.from_owner_mutation(principal, settings)
        authorize.require(ctx, "manage_credentials")
        if not x_step_up:
            raise api.LedgerError("step_up_required", reason="step_up_missing")
        step_up.verify(x_step_up, session_hash=principal.session_hash, now=int(time.time()),
                       settings=settings)
        body = parse_object(raw, {"label", "kind"}, frozenset({"expires_in_days"}))
        return service_for(settings).issue(
            ctx, label=body["label"], kind=body["kind"],
            expires_in_days=body.get("expires_in_days"))

    return await respond(run)


@router.post(ROOT + "/assistant-credentials/revoke-all")
async def revoke_all_keys(
    request: Request,
    principal: OwnerSessionPrincipal = Depends(approve_mutation),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    raw = await request.body()

    def run() -> dict[str, Any]:
        gate(settings)
        ctx = authorize.from_owner_mutation(principal, settings)
        empty_object(raw)
        return service_for(settings).revoke_all(ctx)

    return await respond(run)


@router.post(ROOT + "/assistant-credentials/{credential_id}/revoke")
async def revoke_key(
    credential_id: str,
    request: Request,
    principal: OwnerSessionPrincipal = Depends(approve_mutation),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    raw = await request.body()

    def run() -> dict[str, Any]:
        gate(settings)
        ctx = authorize.from_owner_mutation(principal, settings)
        empty_object(raw)
        return service_for(settings).revoke(ctx, credential_id)

    return await respond(run)
