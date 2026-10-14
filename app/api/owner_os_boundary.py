"""Private-route HTTP bounds and sanitized errors; public surfaces are unchanged."""

import json
from uuid import uuid4

from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.core.owner_timing import RequestTiming

PREFIXES = ("/v1/owner/auth", "/v1/owner/os")


def private_path(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in PREFIXES)


def error_response(status: int, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status,
        content={"api_version": "owner-os.v1", "request_id": uuid4().hex,
                 "error": {"code": code, "retryable": status in {429, 503, 504}}},
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

        if scope["method"] == "POST":
            chunks = []
            size = 0
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                chunk = message.get("body", b"")
                size += len(chunk)
                if size > 16_384:
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
                await error_response(503, "source_unavailable")(scope, receive, private_send)
