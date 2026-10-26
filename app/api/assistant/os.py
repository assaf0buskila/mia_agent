"""Assistant OS bearer routes (V2_PG_CONTRACTS.md §17.2; V2-37).

Three routes under ``/v1/assistant/os`` for an assistant (Claude or GPT) holding a static key:

* ``GET /context``: the business merchant rules and the last 12 statements, plus the batch format;
* ``POST /statement-batches``: submit one ``statement-batch.v1`` (at most 128 KiB). It lands as a
  PENDING V2-26 proposal that only the owner can approve in the OS;
* ``GET /statement-batches/{id}``: the state of one of this key's own batches.

An assistant can only propose. There is no route here that approves, rejects, drafts, commits or
manages keys, and the ledger context an assistant carries is denied each of those by the §9 matrix.
No bearer body contains a decision id.

The order is §17.2's: the feature switch, a cookie (401), the exact header, the per-address count
(429, before any lookup), the hash lookup, validity (one uniform 401), the per-key limits (429),
then ``from_assistant`` / ``require(op)`` inside the V2-26 service. ``/v1/assistant`` is outside
``OwnerOSBoundary``, so the 128 KiB body cap is enforced here by a streamed read: over it is a 422
``validation_error`` that is counted and audited like any other call.

Every refusal is a ``LedgerError`` envelope. A 401 carries ``WWW-Authenticate: Bearer`` and a 429
carries ``Retry-After``. Responses are never cached. Nothing here logs a header, a body or an
address.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.api.os_v2.errors import ledger_error_response
from app.core.config import Settings, get_settings
from app.core.public_website import client_ip
from app.services import assistant_keys as keys
from app.services.os_ledger import api
from app.services.os_ledger.statements import (
    CLAUDE,
    GPT,
    LedgerStatementService,
    build_statement_service,
)

ROOT = "/v1/assistant/os"
BODY_CAP = 131_072  # 128 KiB, §17.2 / §10.5
SOURCES = {"ASSISTANT_CLAUDE": CLAUDE, "ASSISTANT_GPT": GPT}
SUMMARY_KEYS = ("line_count", "business_count", "personal_count", "unassigned_count")
_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}
_RETRY_AFTER = {
    "key_hour_limit": keys.KEY_WINDOW_SECONDS, "key_day_limit": keys.BATCH_WINDOW_SECONDS,
}
_LOG = logging.getLogger("mia.os_assistant")

router = APIRouter()


def service_for(settings: Settings) -> LedgerStatementService:
    """V2-26's service on the PostgreSQL ledger. Tests replace this one function."""
    return build_statement_service(settings)


def gateway_for(settings: Settings) -> keys.AssistantGateway:
    """The bearer gateway over the ledger runtime store. Tests replace this one function."""
    return keys.build_gateway(settings, service_for(settings))


def review_origin(settings: Settings) -> str:
    """The OS origin for ``review_url``, checked BEFORE a batch is submitted (a failure after the
    ledger commit would be wrong): an https origin with no userinfo, query, fragment or path."""
    origin = str(getattr(settings, "owner_os_origin", "") or "").strip()
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username is not None
            or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        _LOG.warning("os_assistant op=submit outcome=refused reason=owner_origin_unset")
        raise api.LedgerError("ledger_unavailable", reason="owner_origin_unset")
    return origin.rstrip("/")


def context_body(context: dict[str, Any]) -> dict[str, Any]:
    """``GET /context``: the business rules, the last 12 statements and the batch format."""
    return {"api_version": keys.API_VERSION, **context}


def submitted_body(origin: str, submitted: Any) -> dict[str, Any]:
    """``POST /statement-batches``: the pending proposal and where the owner reviews it. The
    summary is the submitted lines only; no decision id and no merge, void or replace count."""
    return {
        "api_version": keys.API_VERSION, "proposal_id": submitted.proposal_id,
        "status": submitted.status,
        "review_url": f"{origin}/#statements/{submitted.proposal_id}",
        "summary": {name: int(submitted.summary[name]) for name in SUMMARY_KEYS},
    }


def status_body(status: dict[str, Any]) -> dict[str, Any]:
    """``GET /statement-batches/{id}``: the state of one of this key's own batches."""
    return {"api_version": keys.API_VERSION, **status}


def _error(exc: api.LedgerError, *, write: bool) -> JSONResponse:
    response = ledger_error_response(exc, write=write)
    if exc.code == "assistant_auth_required":
        response.headers["WWW-Authenticate"] = "Bearer"
    elif exc.code == "rate_limited":
        window = _RETRY_AFTER.get(exc.reason or "", keys.IP_WINDOW_SECONDS)
        response.headers["Retry-After"] = str(window)
    return response


async def _read_body(request: Request) -> tuple[bytes, int, bool]:
    """The body, its size and whether it is over the cap. Streamed: it never holds more than the
    cap, and an oversized body is not read to the end."""
    declared = request.headers.get("content-length", "")
    if declared.isascii() and declared.isdigit() and int(declared) > BODY_CAP:
        return b"", min(int(declared), 2_147_483_647), True
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > BODY_CAP:
            return b"", size, True
        chunks.append(chunk)
    return b"".join(chunks), size, False


def _json_content_type(request: Request) -> bool:
    return request.headers.get("content-type", "").partition(";")[0].strip().lower() == (
        "application/json")


async def _serve(
    request: Request, settings: Settings, operation: str, *, body: bool,
    make_work: Callable[[keys.AssistantGateway, keys.Caller, tuple[bytes, int, bool]],
                        Callable[[keys.Facts], dict[str, Any]]],
) -> JSONResponse:
    write = request.method != "GET"
    try:
        keys.check_enabled(settings)  # a disabled feature touches no ledger and no database
        gateway = gateway_for(settings)
        caller = await run_in_threadpool(
            gateway.authenticate, cookie="cookie" in request.headers,
            authorization=request.headers.getlist("authorization"),
            ip=client_ip(request, settings=settings))
        payload = await _read_body(request) if body else (b"", 0, False)
        result = await run_in_threadpool(
            gateway.run, caller, operation, make_work(gateway, caller, payload))
    except api.LedgerError as exc:
        return _error(exc, write=write)
    except Exception:  # noqa: BLE001 - the envelope is the contract; the reason is logged only
        _LOG.warning("os_assistant op=%s outcome=refused reason=unexpected_error", operation)
        return _error(api.LedgerError("unknown" if write else "ledger_unavailable",
                                      reason="unexpected_error"), write=write)
    return JSONResponse(result, status_code=200, headers=_HEADERS)


@router.get(ROOT + "/context")
async def get_context(request: Request, settings: Settings = Depends(get_settings),
                      ) -> JSONResponse:
    def make_work(gateway, caller, _payload):
        def run(facts: keys.Facts) -> dict[str, Any]:
            return context_body(gateway.statements.context_for_assistant(caller.ctx))

        return run

    return await _serve(request, settings, "context", body=False, make_work=make_work)


@router.post(ROOT + "/statement-batches")
async def submit_batch(request: Request, settings: Settings = Depends(get_settings),
                       ) -> JSONResponse:
    json_body = _json_content_type(request)

    def make_work(gateway, caller, payload):
        raw, size, too_big = payload

        def run(facts: keys.Facts) -> dict[str, Any]:
            facts.bytes = size
            if too_big:
                raise api.LedgerError("validation_error", details={"reason": "body_too_large"},
                                      reason="body_too_large")
            if not json_body:
                raise api.LedgerError("validation_error", details={"reason": "content_type"},
                                      reason="content_type")
            origin = review_origin(settings)
            submitted = gateway.statements.submit(
                caller.ctx, raw, source=SOURCES[caller.ctx.channel])
            facts.line_count = int(submitted.summary["line_count"])
            facts.proposal_id = submitted.proposal_id
            return submitted_body(origin, submitted)

        return run

    return await _serve(request, settings, "submit", body=True, make_work=make_work)


@router.get(ROOT + "/statement-batches/{proposal_id}")
async def batch_status(proposal_id: str, request: Request,
                       settings: Settings = Depends(get_settings)) -> JSONResponse:
    def make_work(gateway, caller, _payload):
        def run(facts: keys.Facts) -> dict[str, Any]:
            body = gateway.statements.status_for_credential(caller.ctx, proposal_id)
            facts.proposal_id = body["proposal_id"]
            return status_body(body)

        return run

    return await _serve(request, settings, "status", body=False, make_work=make_work)
