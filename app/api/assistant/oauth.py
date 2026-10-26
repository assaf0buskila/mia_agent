"""Pinned public OAuth discovery and bounded authorization-code transport.

The shared bearer slot covers IP admission, parsing and the complete database worker.
Owner approval and token issuance live in the service, never in this public router.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qsl

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from app.api.assistant import os as rest
from app.core.config import Settings, get_settings
from app.core.public_website import client_ip
from app.services import assistant_keys as keys
from app.services import assistant_oauth as oauth
from app.services.os_ledger import api

router = APIRouter()
BODY_CAP = 4096
BODY_TIMEOUT_SECONDS = 5
_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"}
_LOG = logging.getLogger("mia.os_assistant")
_ERRORS = {"invalid_request", "invalid_grant", "invalid_client", "invalid_scope",
           "unsupported_grant_type"}


def service_for(settings: Settings) -> oauth.AssistantOAuthService:
    return oauth.AssistantOAuthService(api.build_ledger(settings), settings)


def _oauth_error(code: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": code}, status_code=status, headers=_HEADERS)


def _unavailable(exc: api.LedgerError) -> JSONResponse:
    response = _oauth_error("temporarily_unavailable", 429 if exc.code == "rate_limited" else 503)
    if exc.code == "rate_limited":
        response.headers["Retry-After"] = str(keys.IP_WINDOW_SECONDS)
    return response


def _invalid_authorization() -> PlainTextResponse:
    return PlainTextResponse("Invalid authorization request.", status_code=400, headers=_HEADERS)


def _params(raw: bytes) -> dict[str, str]:
    if len(raw) > BODY_CAP:
        raise ValueError("body_too_large")
    value = raw.decode("utf-8", errors="strict")
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError("invalid_encoding")
    pairs = parse_qsl(value, keep_blank_values=True, strict_parsing=True,
                      encoding="utf-8", errors="strict", max_num_fields=32)
    result = {}
    for name, item in pairs:
        if name in result:
            raise ValueError("duplicate_parameter")
        result[name] = item
    return result


async def _form_body(request: Request) -> tuple[bytes, int, bool]:
    return await rest._stream_body(request, BODY_CAP)


def _metadata(settings: Settings, *, protected: bool) -> JSONResponse:
    try:
        oauth.enabled(settings)
        base = oauth.issuer(settings)
        if protected:
            body = {"resource": oauth.resource(settings), "authorization_servers": [base],
                    "scopes_supported": [oauth.SCOPE], "bearer_methods_supported": ["header"]}
        else:
            body = {"issuer": base, "authorization_endpoint": base + "/oauth/authorize",
                    "token_endpoint": base + "/oauth/token", "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code"],
                    "code_challenge_methods_supported": ["S256"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "scopes_supported": [oauth.SCOPE],
                    "client_id_metadata_document_supported": True,
                    "authorization_response_iss_parameter_supported": True}
        return JSONResponse(body, headers={"Cache-Control": "public, max-age=300"})
    except api.LedgerError as exc:
        return _unavailable(exc)


@router.get("/.well-known/oauth-protected-resource")
@router.get("/.well-known/oauth-protected-resource/v1/assistant/mcp")
async def protected_resource(settings: Settings = Depends(get_settings)) -> JSONResponse:
    return _metadata(settings, protected=True)


@router.get("/.well-known/oauth-authorization-server")
@router.get("/.well-known/openid-configuration")
async def authorization_server(settings: Settings = Depends(get_settings)) -> JSONResponse:
    return _metadata(settings, protected=False)


@router.get("/oauth/authorize")
@router.get("/oauth/authorize/{suffix:path}", include_in_schema=False)
async def authorize(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    try:
        oauth.enabled(settings)
        async with keys.async_bearer_request():
            service = service_for(settings)
            await rest._bearer_threadpool(service.account, client_ip(request, settings=settings),
                                         "oauth_authorize")
            if request.scope["path"] != "/oauth/authorize":
                return _invalid_authorization()
            params = _params(request.scope.get("query_string", b""))
            origin = rest.review_origin(settings)
            row = await rest._bearer_threadpool(service.authorize, params)
            return RedirectResponse(origin + "/#connect/" + row.oauth_request_id,
                                    status_code=302, headers=_HEADERS)
    except (ValueError, oauth.OAuthError):
        return _invalid_authorization()
    except api.LedgerError as exc:
        return _unavailable(exc)
    except Exception:  # noqa: BLE001 - no query, form, provider or exception payload is logged
        _LOG.warning("os_assistant op=oauth_authorize outcome=refused reason=unexpected_error")
        return _oauth_error("temporarily_unavailable", 503)


@router.post("/oauth/token")
@router.post("/oauth/token/{suffix:path}", include_in_schema=False)
async def token(request: Request, settings: Settings = Depends(get_settings)) -> JSONResponse:
    try:
        oauth.enabled(settings)
        form_body = request.headers.get("content-type", "").partition(";")[0].strip().lower() == (
            "application/x-www-form-urlencoded")
        raw, _size, outcome = (await rest._read_outcome(request, _form_body, BODY_TIMEOUT_SECONDS)
                               if form_body else (b"", 0, "ok"))
        async with keys.async_bearer_request():
            service = service_for(settings)
            accounting = {"reason": "body_timeout"} if outcome in {"timeout", "disconnect"} else {}
            await rest._bearer_threadpool(service.account, client_ip(request, settings=settings),
                                         "oauth_token", **accounting)
            if request.scope["path"] != "/oauth/token":
                return _oauth_error("invalid_request")
            if not form_body or outcome != "ok":
                return _oauth_error("invalid_request")
            params = _params(raw)
            result = await rest._bearer_threadpool(service.token, params)
            return JSONResponse(result, headers=_HEADERS)
    except ValueError:
        return _oauth_error("invalid_request")
    except oauth.OAuthError as exc:
        return _oauth_error(exc.code if exc.code in _ERRORS else "invalid_request")
    except api.LedgerError as exc:
        return _unavailable(exc)
    except Exception:  # noqa: BLE001 - never log a code, verifier, token or exception payload
        _LOG.warning("os_assistant op=oauth_token outcome=refused reason=unexpected_error")
        return _oauth_error("temporarily_unavailable", 503)
