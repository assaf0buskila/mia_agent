"""Owner step-up re-authentication (V2_PG_CONTRACTS.md §17.1, FORK-STEP-UP; V2-37).

``POST /v1/owner/auth/step-up`` with ``{id_token}``. It is its own router under the same prefix as
the session routes, so ``owner_sessions.py`` is untouched. It needs an owner session, the exact
Origin and CSRF with the ``assaf-os:approve`` scope (the §9 ``manage_credentials`` cell).

The Google token must be a fresh re-authentication: ``verify_step_up_id_token`` accepts it only when
its ``auth_time`` is at most 600 s old (the caller asks Google for ``max_age=0``), and the token's
nonce must be this session's CSRF token, which ties the proof to this browser session. The answer is
exactly ``{step_up_token, expires_at}``: an HMAC token over the session hash, valid ten minutes,
that ``POST /assistant-credentials`` then presents in ``X-Step-Up``.

A token Google or the checks refuse is a 403 ``step_up_required`` (the owner's session is fine, the
proof is not); an unconfigured secret or Google binding, or an unreachable key source, is a 503
``ledger_unavailable``. Nothing here logs the Google token, the step-up token or the CSRF token.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse

from app.api.os_v2.owner_keys import approve_mutation, gate, parse_object, respond
from app.api.owner_sessions import CSRF_HEADER, OwnerSessionPrincipal
from app.core.config import Settings, get_settings
from app.services import assistant_step_up as step_up
from app.services import owner_google_login as google
from app.services.os_ledger import api, authorize

MAX_ID_TOKEN_CHARS = 6144
_LOG = logging.getLogger("mia.os_step_up")

router = APIRouter(prefix="/v1/owner/auth")


def _google_configured(settings: Settings) -> bool:
    return bool(
        getattr(settings, "owner_os_google_enabled", False) is True
        and str(settings.owner_os_google_client_id).strip()
        and str(settings.owner_os_google_subject).strip()
        and str(settings.owner_os_google_owner_id).strip()
    )


@router.post("/step-up")
async def step_up_route(
    request: Request,
    csrf_token: Annotated[str | None, Header(alias=CSRF_HEADER)] = None,
    principal: OwnerSessionPrincipal = Depends(approve_mutation),
    settings: Settings = Depends(get_settings),
) -> JSONResponse:
    raw = await request.body()

    def run() -> dict[str, Any]:
        gate(settings)
        ctx = authorize.from_owner_mutation(principal, settings)
        authorize.require(ctx, "manage_credentials")
        body = parse_object(raw, {"id_token"})
        id_token = body["id_token"]
        if type(id_token) is not str or not 0 < len(id_token) <= MAX_ID_TOKEN_CHARS:
            raise api.LedgerError("validation_error", details={"reason": "invalid_body"},
                                  reason="invalid_body")
        step_up.ensure_configured(settings)
        if not _google_configured(settings):
            _LOG.warning("os_step_up op=google outcome=refused reason=google_unconfigured")
            raise api.LedgerError("ledger_unavailable", reason="google_unconfigured")
        now = int(time.time())
        try:
            # The nonce is the session's CSRF token (already validated against the cookie).
            auth_time = google.verify_step_up_id_token(
                id_token, settings=settings, now=now, nonce=csrf_token)
        except google.GoogleOwnerLoginDenied:
            _LOG.info("os_step_up op=google outcome=step_up_required reason=google_proof")
            raise api.LedgerError("step_up_required", reason="google_proof") from None
        except google.GoogleJwksUnavailable:
            _LOG.warning("os_step_up op=google outcome=refused reason=google_keys_unavailable")
            raise api.LedgerError("ledger_unavailable", reason="google_keys_unavailable") from None
        minted = step_up.mint(session_hash=principal.session_hash, auth_time=auth_time, now=now,
                              settings=settings)
        return step_up.body(minted)

    return await respond(run)
