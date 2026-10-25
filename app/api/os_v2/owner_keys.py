"""Assaf OS v2 owner routes for the assistant keys (V2_PG_CONTRACTS.md §17.1, §9, §10.5; V2-37).

Under ``/v1/owner/os/unified`` (the module name is erratum E34's; the URL is unchanged):

* ``GET  /assistant-credentials``: the list, no secret;
* ``POST /assistant-credentials`` with ``{label, kind, expires_in_days?}``: commits a pending
  E37 request and immutable Telegram card, then sends the card and returns 202;
* ``GET /assistant-credentials/issue-requests/{id}``: status for the requesting session;
* ``POST .../issue-requests/{id}/collect``: after Telegram approval, that same verified OS
  session collects the list item and one-time key within ten minutes;
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
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.api import owner_sessions
from app.api.deps import get_db, get_telegram_port
from app.api.owner_sessions import (
    CSRF_HEADER,
    OwnerSessionPrincipal,
    get_owner_session_db,
    owner_scope,
)
from app.capabilities.types import Principal
from app.core.config import Settings, get_settings
from app.db.store import LeadStore
from app.domain.owner.callbacks import approval_token
from app.domain.owner.proposal_cards import render_owner_approval_card
from app.integrations.base import MessagePort, OutboundMessage
from app.integrations.telegram_format import approval_keyboard
from app.services.assistant_issue import AssistantIssueService
from app.services.os_ledger import api, authorize
from app.services.owner_actions import propose_owner_action

ROOT = "/v1/owner/os/unified"
APPROVE_SCOPE = "assaf-os:approve"
HEADERS = {"Cache-Control": "private, no-store"}
_LOG = logging.getLogger("mia.os_v2")

router = APIRouter()


def service_for(settings: Settings) -> AssistantIssueService:
    """The owner's key service on the PostgreSQL ledger. Tests replace this one function."""
    return AssistantIssueService(api.build_ledger(settings), settings)


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


def login_chain(db: Session, principal: OwnerSessionPrincipal) -> str:
    """Bind to the authenticated login across cookie rotation; never fall back to the cookie."""
    chain = db.execute(text(
        "SELECT chain_id FROM owner_os_sessions WHERE session_hash = :h"
    ), {"h": principal.session_hash}).scalar_one_or_none()
    if type(chain) is not str or not chain:
        raise api.LedgerError("owner_auth_required", reason="session_chain_missing")
    return chain


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


@router.post(ROOT + "/assistant-credentials", status_code=202)
async def issue_key(
    request: Request,
    principal: OwnerSessionPrincipal = Depends(approve_mutation),
    settings: Settings = Depends(get_settings),
    db: Session = Depends(get_db),
    session_db: Session = Depends(get_owner_session_db),
    port: MessagePort = Depends(get_telegram_port),
) -> JSONResponse:
    raw = await request.body()

    def run():
        gate(settings)
        ctx = authorize.from_owner_mutation(principal, settings)
        authorize.require(ctx, "manage_credentials")
        body = parse_object(raw, {"label", "kind"}, frozenset({"expires_in_days"}))
        service = service_for(settings)
        chain = login_chain(session_db, principal)
        pending = service.issue(
            ctx, chain_id=chain, label=body["label"], kind=body["kind"],
            expires_in_days=body.get("expires_in_days"))
        envelope = service.proposal(ctx, pending["issue_request_id"],
                                    chain_id=chain)
        store = LeadStore(db)
        action = propose_owner_action(
            store, principal=Principal.owner(source="telegram", actor_id=principal.owner_id),
            source_ref=pending["issue_request_id"], kind="assistant_key.issue",
            parameters=envelope["parameters"], target=envelope["target"], ttl_seconds=600)
        db.commit()  # the ledger request and the card both commit before any Telegram call
        card = render_owner_approval_card(store.get_approval_by_approval_id(action.approval_id))
        db.commit()  # rendering's read transaction is closed before the network call, too
        return pending, action.approval_id, card

    pending, approval_id, card = await run_in_threadpool(run)
    try:
        await port.send(OutboundMessage(
            conversation_id=principal.owner_id, text=card, channel="telegram",
            idempotency_key=f"assistant-issue:{approval_id}", parse_mode="HTML",
            reply_markup=approval_keyboard(approval_token(approval_id))))
    except Exception:
        _LOG.warning("os_v2 op=assistant_issue outcome=pending reason=card_send_failed")
    return JSONResponse(pending, status_code=202, headers=HEADERS)


@router.get(ROOT + "/assistant-credentials/issue-requests/{issue_request_id}")
async def issue_status(
    issue_request_id: str,
    principal: OwnerSessionPrincipal = Depends(owner_scope("assaf-os:read")),
    settings: Settings = Depends(get_settings),
    session_db: Session = Depends(get_owner_session_db),
) -> JSONResponse:
    def run():
        gate(settings)
        return service_for(settings).status(authorize.from_owner_read(principal, settings),
                                             issue_request_id,
                                             chain_id=login_chain(session_db, principal))
    return await respond(run)


@router.post(ROOT + "/assistant-credentials/issue-requests/{issue_request_id}/collect")
async def collect_key(
    issue_request_id: str,
    request: Request,
    principal: OwnerSessionPrincipal = Depends(approve_mutation),
    settings: Settings = Depends(get_settings),
    session_db: Session = Depends(get_owner_session_db),
) -> JSONResponse:
    raw = await request.body()

    def run():
        gate(settings)
        empty_object(raw)
        return service_for(settings).collect(authorize.from_owner_mutation(principal, settings),
                                              issue_request_id,
                                              chain_id=login_chain(session_db, principal))
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
