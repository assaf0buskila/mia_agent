"""Private-route HTTP bounds and sanitized errors; public surfaces are unchanged."""

import json
import logging
import re
from contextlib import nullcontext
from time import monotonic
from uuid import uuid4

from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.core.owner_timing import (
    RequestTiming,
    log_owner_os_request,
    request_id_or_new,
    request_id_scope,
)
from app.core.owner_write import WriteDispatch, current_write_dispatch

PREFIXES = ("/v1/owner/auth", "/v1/owner/os")
_WRITE_ROUTE = re.compile(r"/v1/owner/os/unified/proposals(?:/[^/]+/(approve|reject))?")
_UNIFIED = r"/v1/owner/os/unified"
# (method, path) -> the op named on the per-request summary line. Fixed names only: the raw path
# (which carries proposal and request ids) is never logged.
_OPS = (
    ("GET", re.compile(_UNIFIED), "snapshot"),
    ("GET", re.compile(_UNIFIED + r"/schema"), "schema"),
    ("GET", re.compile(_UNIFIED + r"/cached"), "cached"),
    ("POST", re.compile(_UNIFIED + r"/proposals"), "propose"),
    ("GET", re.compile(_UNIFIED + r"/proposals/[^/]+"), "proposal"),
    ("POST", re.compile(_UNIFIED + r"/proposals/[^/]+/approve"), "approve"),
    ("POST", re.compile(_UNIFIED + r"/proposals/[^/]+/reject"), "reject"),
    ("GET", re.compile(_UNIFIED + r"/receipts/[^/]+"), "receipt"),
    ("GET", re.compile(_UNIFIED + r"/pending"), "pending"),
    ("GET", re.compile(_UNIFIED + r"/proposal-requests/[^/]+"), "proposal_request"),
    # V2-24 owner-direct commits.
    ("POST", re.compile(_UNIFIED + r"/commits"), "commit"),
    ("POST", re.compile(_UNIFIED + r"/commits/[^/]+/undo"), "undo"),
    ("POST", re.compile(_UNIFIED + r"/commits/[^/]+/abandon"), "abandon"),
    ("GET", re.compile(_UNIFIED + r"/deleted"), "deleted"),
)
_AUTH_PREFIX = "/v1/owner/auth"
_LOG = logging.getLogger("mia.owner_write")
# Body caps per route (V2_PG_CONTRACTS.md §10.5); every other private body keeps 16 KiB.
_DEFAULT_BODY_CAP = 16_384
_BODY_CAPS = (
    (re.compile(r"/v1/owner/os/unified/statement-batches"), 131_072),
)


def private_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in PREFIXES)


def body_cap(path: str) -> int:
    for route, cap in _BODY_CAPS:
        if route.fullmatch(path):
            return cap
    return _DEFAULT_BODY_CAP


def request_op(method: str, path: str) -> str:
    """The fixed op name of a private request, for the summary log line."""
    if path == _AUTH_PREFIX or path.startswith(_AUTH_PREFIX + "/"):
        return "auth"
    for verb, route, op in _OPS:
        if method == verb and route.fullmatch(path):
            return op
    return "other"


def error_response(status: int, code: str, *, write_unknown: bool = False) -> JSONResponse:
    error = {"code": code, "retryable": status in {429, 503, 504}}
    dispatch = current_write_dispatch()
    if dispatch is not None and 400 <= status < 600:
        not_started = not write_unknown and dispatch.not_started
        if not_started:
            error["write_outcome"] = "not_started"
        # Signed Script error codes are still external input: never log arbitrary text.
        reason = code if re.fullmatch(r"[a-z][a-z0-9_]{1,47}", code) else "error"
        _LOG.info(
            "owner_os_write op=%s outcome=%s reason=%s",
            dispatch.operation, "not_started" if not_started else "unknown", reason,
        )
    # The envelope id IS the request's id (also the x-request-id header and the log lines'), so a
    # phone-side error joins the server logs. Outside a request it is a fresh random id.
    rid = request_id_or_new()
    return JSONResponse(
        status_code=status,
        content={"api_version": "owner-os.v1", "request_id": rid,
                 "error": error},
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                 "Referrer-Policy": "no-referrer", "Vary": "Cookie, Origin",
                 "X-Request-Id": rid},
    )


async def owner_validation_error(request, exc: RequestValidationError):
    if private_path(request.url.path):
        return error_response(422, "validation_error")
    return await request_validation_exception_handler(request, exc)


async def owner_http_error(request, exc: HTTPException):
    if private_path(request.url.path):
        code = exc.detail if isinstance(exc.detail, str) else "source_unavailable"
        return error_response(exc.status_code, code)
    return await http_exception_handler(request, exc)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


class OwnerOSBoundary:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not private_path(scope["path"]):
            return await self.app(scope, receive, send)
        # Wall time and id are taken before anything else runs. The id is random, is held for the
        # whole request, and names nothing: it joins the log lines to the response and the phone.
        started = monotonic()
        rid = uuid4().hex
        match = _WRITE_ROUTE.fullmatch(scope["path"]) if scope["method"] == "POST" else None
        context = WriteDispatch(match.group(1) or "propose").activate() if match else nullcontext()
        with request_id_scope(rid), context:
            return await self._handle(scope, receive, send, rid=rid, started=started)

    async def _handle(self, scope, receive, send, *, rid, started):
        op = request_op(scope["method"], scope["path"])

        def elapsed_ms() -> int:
            return max(0, int((monotonic() - started) * 1000))

        async def logged_send(message):
            # A response rejected before the timing wrapper exists (it already carries its id).
            if message["type"] == "http.response.start":
                log_owner_os_request(
                    op=op, status=message["status"], total_ms=elapsed_ms(), rid=rid
                )
            await send(message)

        if scope.get("query_string", b"") and scope["path"].startswith("/v1/owner/auth"):
            return await error_response(400, "invalid_query")(scope, receive, logged_send)

        timing = RequestTiming()

        async def private_send(message):
            if message["type"] == "http.response.start":
                # Only our allow-listed value may leave; drop anything a handler set itself.
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in {b"cache-control", b"vary", b"server-timing",
                                                b"x-request-id"}]
                message["headers"] = headers + [
                    (b"cache-control", b"private, no-store"), (b"vary", b"Cookie, Origin"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"x-request-id", rid.encode("ascii")),
                ]
                # One measurement feeds both the header and the log line: the server-side wall
                # time from entry to this first response byte. Empty unless authentication
                # succeeded, and always empty on 401/403.
                elapsed = elapsed_ms()
                timing.add("total", elapsed)
                server_timing = timing.header(message["status"])
                if server_timing:
                    message["headers"].append((b"server-timing", server_timing.encode("ascii")))
                log_owner_os_request(
                    op=op, status=message["status"], total_ms=elapsed, rid=rid
                )
            await send(message)

        # Every non-GET body is buffered, capped and checked before any handler runs.
        if scope["method"] != "GET":
            cap = body_cap(scope["path"])
            chunks = []
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > cap:
                    return await error_response(413, "validation_error")(
                        scope, receive, logged_send
                    )
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            payload = b"".join(chunks)
            if payload:
                headers = dict(scope["headers"])
                if headers.get(b"content-type", b"").split(b";", 1)[0] != b"application/json":
                    return await error_response(415, "validation_error")(
                        scope, receive, logged_send
                    )
                try:
                    value = json.loads(payload, object_pairs_hook=_unique)
                    if not isinstance(value, dict):
                        raise ValueError("object_required")
                except (ValueError, UnicodeError):
                    return await error_response(422, "validation_error")(
                        scope, receive, logged_send
                    )
            delivered = False
            original_receive = receive

            async def buffered_receive():
                nonlocal delivered
                if not delivered:
                    delivered = True
                    return {"type": "http.request", "body": payload, "more_body": False}
                return await original_receive()

            receive = buffered_receive
        with timing.activate():
            try:
                await self.app(scope, receive, private_send)
            except Exception:
                # Never include SQL parameters, credentials, PII or exception messages.
                await error_response(503, "source_unavailable", write_unknown=True)(
                    scope, receive, private_send
                )
