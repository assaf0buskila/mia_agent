"""Render every LedgerError as the V2-05 owner-os envelope (V2_PG_CONTRACTS.md §5.2).

A v2 route's ``write_outcome`` comes only from its LedgerError; v2 routes are never registered with
``WriteDispatch``. ``write_outcome`` is present only on write routes and ``details`` only when the
code allows it. Nothing from the request, the record or the database is rendered or logged.
"""

from __future__ import annotations

import logging
from uuid import uuid4

from fastapi import Request
from fastapi.responses import JSONResponse

from app.services.os_ledger.api import LedgerError

_LOG = logging.getLogger("mia.os_v2")
_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Vary": "Cookie, Origin",
}
_READ_METHODS = frozenset({"GET", "HEAD"})
# Step-up re-authentication is not an OS write (§10.5: no osWrite on /api/auth/step-up).
_NON_WRITE_PREFIX = "/v1/owner/auth"


def write_route(method: str, path: str) -> bool:
    return method.upper() not in _READ_METHODS and not (
        path == _NON_WRITE_PREFIX or path.startswith(_NON_WRITE_PREFIX + "/")
    )


def ledger_error_response(exc: LedgerError, *, write: bool) -> JSONResponse:
    error: dict[str, object] = {"code": exc.code, "retryable": exc.retryable}
    if write:
        error["write_outcome"] = exc.write_outcome
    if exc.details:
        error["details"] = dict(exc.details)
    return JSONResponse(
        status_code=exc.status,
        content={"api_version": "owner-os.v1", "request_id": uuid4().hex, "error": error},
        headers=_HEADERS,
    )


async def ledger_error_handler(request: Request, exc: LedgerError) -> JSONResponse:
    write = write_route(request.method, request.url.path)
    _LOG.info(
        "os_v2 op=%s outcome=%s reason=%s write=%s",
        "write" if write else "read", exc.code, exc.reason or exc.code, write,
    )
    return ledger_error_response(exc, write=write)
