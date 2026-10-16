"""Private-route HTTP bounds and sanitized errors; public surfaces are unchanged."""

import json
import logging
import re
from contextlib import nullcontext
from uuid import uuid4

from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.core.owner_timing import RequestTiming
from app.core.owner_write import WriteDispatch, current_write_dispatch

PREFIXES = ("/v1/owner/auth", "/v1/owner/os")
_WRITE_ROUTE = re.compile(r"/v1/owner/os/unified/proposals(?:/[^/]+/(approve|reject))?")
_LOG = logging.getLogger("mia.owner_write")
# Body caps per route (V2_PG_CONTRACTS.md §10.5); every other private body keeps 16 KiB.
_DEFAULT_BODY_CAP = 16_384
_BODY_CAPS = (
    (re.compile(r"/v1/owner/os/unified/statement-batches"), 131_072),
    (re.compile(r"/v1/owner/auth/step-up"), 8_192),
)


def private_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in PREFIXES)


def body_cap(path: str) -> int:
    for route, cap in _BODY_CAPS:
        if route.fullmatch(path):
            return cap
    return _DEFAULT_BODY_CAP


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
    return JSONResponse(
        status_code=status,
        content={"api_version": "owner-os.v1", "request_id": uuid4().hex,
                 "error": error},
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                 "Referrer-Policy": "no-referrer", "Vary": "Cookie, Origin"},
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
        match = _WRITE_ROUTE.fullmatch(scope["path"]) if scope["method"] == "POST" else None
        context = WriteDispatch(match.group(1) or "propose").activate() if match else nullcontext()
        with context:
            return await self._handle(scope, receive, send)

    async def _handle(self, scope, receive, send):
        if scope.get("query_string", b"") and scope["path"].startswith("/v1/owner/auth"):
            return await error_response(400, "invalid_query")(scope, receive, send)

        timing = RequestTiming()

        async def private_send(message):
            if message["type"] == "http.response.start":
                # Only our allow-listed value may leave; drop anything a handler set itself.
                headers = [(k, v) for k, v in message.get("headers", [])
                           if k.lower() not in {b"cache-control", b"vary", b"server-timing"}]
                message["headers"] = headers + [
                    (b"cache-control", b"private, no-store"), (b"vary", b"Cookie, Origin"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                ]
                # Empty unless authentication succeeded, and always empty on 401/403.
                server_timing = timing.header(message["status"])
                if server_timing:
                    message["headers"].append((b"server-timing", server_timing.encode("ascii")))
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
                    return await error_response(413, "validation_error")(scope, receive, send)
                chunks.append(chunk)
                if not message.get("more_body", False):
                    break
            payload = b"".join(chunks)
            if payload:
                headers = dict(scope["headers"])
                if headers.get(b"content-type", b"").split(b";", 1)[0] != b"application/json":
                    return await error_response(415, "validation_error")(scope, receive, send)
                try:
                    value = json.loads(payload, object_pairs_hook=_unique)
                    if not isinstance(value, dict):
                        raise ValueError("object_required")
                except (ValueError, UnicodeError):
                    return await error_response(422, "validation_error")(scope, receive, send)
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
