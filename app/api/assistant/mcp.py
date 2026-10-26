"""V2-38: stateless JSON-only MCP over V2-37's propose-only assistant gateway.

No session, SSE, owner context, decision entry point, or second limiter. Authentication,
streamed size cap, service admission, key quotas, audit, and response bodies belong to V2-37.
S1/S1b add an IP-counted row for every authenticated non-tool request without a key quota charge.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.api.assistant import os as rest
from app.core.config import Settings, get_settings
from app.core.public_website import client_ip
from app.domain.statements import vocabulary as v
from app.services import assistant_keys as keys
from app.services.os_ledger import api

router = APIRouter()
PROTOCOL_VERSIONS = ("2025-06-18",)
SERVER_VERSION = "1.0.0"
_LOG = logging.getLogger("mia.os_assistant")


def _object(properties: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": properties, "required": required,
            "additionalProperties": False}


# Wire schema only; V2-26 remains the validator, including cross-field semantic checks.
# Tests derive this independently from STATEMENT_BATCH_FORMAT.md's field table.
_DATE = {"type": "string", "format": "date", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
_AMOUNT = {"type": "integer", "minimum": 1}
_LINE = _object({
    "txn_date": _DATE,
    "merchant_display": {"type": "string", "minLength": 1, "maxLength": v.MAX_DISPLAY_CHARS},
    "amount_minor": _AMOUNT, "kind": {"type": "string", "enum": list(v.KINDS)},
    "currency": {"type": "string", "const": v.CURRENCY},
    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    "suggested_tag": {"enum": [*v.TAGS, None]},
    "original_amount_minor": _AMOUNT,
    "original_currency": {"type": "string", "pattern": "^[A-Z]{3}$",
                          "not": {"const": v.CURRENCY}},
    "installment": _object({"n": {"type": "integer", "minimum": 1},
                            "of": {"type": "integer", "minimum": 1}}, ["n", "of"]),
    "suggested_category": {"type": "string", "maxLength": v.MAX_CATEGORY_CHARS},
    "suggested_path": {"type": "string", "enum": list(v.PATHS)},
    "is_subscription": {"type": "boolean"},
}, ["txn_date", "merchant_display", "amount_minor", "kind", "currency", "confidence",
    "suggested_tag"])
_LINE["dependentRequired"] = {"original_amount_minor": ["original_currency"],
                              "original_currency": ["original_amount_minor"]}
BATCH_SCHEMA = _object({
    "schema": {"type": "string", "const": v.SCHEMA_VERSION},
    "client_batch_id": {"type": "string", "pattern":
                        "^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"},
    "account_label": {"type": "string", "minLength": 1, "maxLength": v.MAX_ACCOUNT_LABEL_CHARS,
                      "pattern": r"^(?!.*\d{8}).*$"},
    "charge_date": _DATE,
    "lines": {"type": "array", "minItems": 1, "maxItems": v.MAX_LINES, "items": _LINE},
}, ["schema", "client_batch_id", "account_label", "charge_date", "lines"])
TOOLS = [
    {"name": "get_statement_context", "description": "Read business rules and statement context.",
     "inputSchema": _object({}, [])},
    {"name": "submit_statement_batch", "description":
     "Propose a statement batch. Only Assaf can approve it in the OS; nothing executes here.",
     "inputSchema": _object({"batch": BATCH_SCHEMA}, ["batch"])},
    {"name": "get_batch_status", "description": "Read the status of this key's own batch.",
     "inputSchema": _object({"proposal_id": {"type": "string"}}, ["proposal_id"])},
]
_OPERATIONS = {"get_statement_context": "context", "submit_statement_batch": "submit",
               "get_batch_status": "status"}
_CONTROLS = {"initialize": "initialize", "ping": "ping", "tools/list": "tools_list"}


class _ToolError(api.LedgerError):
    """A transport argument error, counted through the gateway's control seam."""

    def __init__(self, wire_code: str) -> None:
        super().__init__("validation_error", reason=wire_code)
        self.wire_code = wire_code


def _rpc_error(request_id: Any, code: int, message: str) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id,
                         "error": {"code": code, "message": message}}, headers=rest._HEADERS)


def _reply(request_id: Any, result: dict) -> JSONResponse:
    return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result},
                        headers=rest._HEADERS)


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("duplicate_key")
        result[name] = value
    return result


def _reject_constant(_value: str) -> Any:
    raise ValueError("non_finite_number")


def _tool_work(gateway, caller, settings, params, size):
    # Reject transport errors before gateway.run: no tool/service work means one S1b control row.
    if not isinstance(params, dict):
        raise _ToolError("invalid_params")
    name = params.get("name")
    if not isinstance(name, str) or name not in _OPERATIONS:
        raise _ToolError("method_not_found")
    if set(params) - {"name", "arguments", "_meta"}:
        raise _ToolError("invalid_params")
    args = params.get("arguments", {})
    required = {"context": set(), "submit": {"batch"}, "status": {"proposal_id"}}[
        _OPERATIONS[name]]
    if not isinstance(args, dict) or set(args) != required:
        raise _ToolError("invalid_params")
    if name == "get_batch_status" and not isinstance(args["proposal_id"], str):
        raise _ToolError("invalid_params")

    def run(facts: keys.Facts) -> dict:
        facts.bytes = size
        if name == "get_statement_context":
            return rest.context_body(gateway.statements.context_for_assistant(caller.ctx))
        if name == "submit_statement_batch":
            origin = rest.review_origin(settings)
            submitted = gateway.statements.submit(
                caller.ctx, args["batch"], source=rest.SOURCES[caller.ctx.channel])
            facts.line_count = int(submitted.summary["line_count"])
            facts.proposal_id = submitted.proposal_id
            return rest.submitted_body(origin, submitted)
        status = gateway.statements.status_for_credential(caller.ctx, args["proposal_id"])
        facts.proposal_id = status["proposal_id"]
        return rest.status_body(status)

    return run


@router.post("/v1/assistant/mcp")
async def mcp(request: Request, settings: Settings = Depends(get_settings)) -> Response:
    if "origin" in request.headers:
        return JSONResponse({"error": {"code": "origin_denied"}}, status_code=403,
                            headers=rest._HEADERS)
    try:
        keys.check_enabled(settings)
        gateway = rest.gateway_for(settings)
        caller = await run_in_threadpool(
            gateway.authenticate, cookie="cookie" in request.headers,
            authorization=request.headers.getlist("authorization"),
            ip=client_ip(request, settings=settings))
        raw, size, too_big = await rest._read_body(request)
        if too_big:
            await run_in_threadpool(gateway.control, caller, "body_too_large")
            raise api.LedgerError("validation_error", details={"reason": "body_too_large"},
                                  reason="body_too_large")
        if not rest._json_content_type(request):
            await run_in_threadpool(gateway.control, caller, "unsupported_media")
            raise api.LedgerError("validation_error", details={"reason": "content_type"},
                                  reason="content_type")
        try:
            message = json.loads(raw, object_pairs_hook=_strict_pairs,
                                 parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            await run_in_threadpool(gateway.control, caller, "invalid_request")
            return _rpc_error(None, -32700, "parse_error")
        if (not isinstance(message, dict) or message.get("jsonrpc") != "2.0"
                or not isinstance(message.get("method"), str)
                or ("id" in message and type(message["id"]) not in (str, int))):
            await run_in_threadpool(gateway.control, caller, "invalid_request")
            return _rpc_error(None, -32600, "invalid_request")
        request_id = message.get("id")
        # Notifications cannot invoke tools or mutate anything. Auth still ran above.
        if "id" not in message:
            await run_in_threadpool(gateway.control, caller, "notification")
            return Response(status_code=202, headers=rest._HEADERS)
        method = message["method"]
        params = message.get("params", {})
        if method != "tools/call" and not isinstance(params, dict):
            await run_in_threadpool(gateway.control, caller, "invalid_params")
            return _rpc_error(request_id, -32602, "invalid_params")
        if method in _CONTROLS:
            await run_in_threadpool(gateway.control, caller, _CONTROLS[method])
            if method == "initialize":
                requested = params.get("protocolVersion")
                return _reply(request_id, {
                    "protocolVersion": requested if requested in PROTOCOL_VERSIONS
                    else PROTOCOL_VERSIONS[-1],
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "assaf-os", "version": SERVER_VERSION}})
            return _reply(request_id, {"tools": TOOLS} if method == "tools/list" else {})
        if method != "tools/call":
            await run_in_threadpool(gateway.control, caller, "method_not_found")
            return _rpc_error(request_id, -32601, "method_not_found")
        try:
            work = _tool_work(gateway, caller, settings, params, size)
        except _ToolError as exc:
            await run_in_threadpool(gateway.control, caller, exc.wire_code)
            return _reply(request_id, {"isError": True,
                                       "content": [{"type": "text", "text": exc.wire_code}]})
        try:
            result = await run_in_threadpool(
                gateway.run, caller, _OPERATIONS[params["name"]], work)
        except api.LedgerError as exc:
            return _reply(request_id, {"isError": True,
                                       "content": [{"type": "text", "text": exc.code}]})
        except Exception:  # noqa: BLE001 - no exception text crosses the transport
            _LOG.warning("os_assistant op=mcp_call outcome=refused reason=unexpected_error")
            return _reply(request_id, {"isError": True,
                                       "content": [{"type": "text", "text": "unknown"}]})
        return _reply(request_id, {"content": [{"type": "text", "text": json.dumps(
            result, ensure_ascii=False, separators=(",", ":"))}], "structuredContent": result})
    except api.LedgerError as exc:
        return rest._error(exc, write=True)
    except Exception:  # noqa: BLE001 - V2-37's uniform, payload-free outer envelope
        _LOG.warning("os_assistant op=mcp outcome=refused reason=unexpected_error")
        return rest._error(api.LedgerError("unknown", reason="unexpected_error"), write=True)
