"""V2-38: stateless transport and the propose-only security boundary, through the real app."""

import inspect
import json

import pytest
import yaml
from app.api.assistant import os as rest
from app.services import assistant_keys as keys
from app.services.os_ledger.core import PgLedger
from app.services.os_ledger.statement_tables import RuleRow, tables_for
from app.services.os_ledger.statements import LedgerStatementService
from tests.api.assistant.format_schema import ROOT, object_schema, schema_from_format
from tests.services.os_ledger.statements.support import batch, line, three_lines

PATH = "/v1/assistant/mcp"
METHODS = ("initialize", "ping", "tools/list", "tools/call")


def rpc(client, key=None, method="ping", params=None, **kwargs):
    headers = kwargs.pop("headers", {})
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {"jsonrpc": "2.0", "id": 7, "method": method}
    if params is not None:
        body["params"] = params
    return client.post(PATH, json=body, headers=headers, **kwargs)


def call(client, key, name, arguments=None):
    return rpc(client, key, "tools/call", {"name": name, "arguments": arguments or {}})


def result(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == "application/json"
    assert "mcp-session-id" not in response.headers
    assert response.json()["id"] == 7
    return response.json()["result"]


def body(response):
    value = result(response)
    assert value.get("isError", False) is False
    assert json.loads(value["content"][0]["text"]) == value["structuredContent"]
    return value["structuredContent"]


def test_exactly_three_tools_and_doc_derived_schemas(env):
    aw, client = env
    tools = result(rpc(client, aw.issue()["secret"], "tools/list"))["tools"]
    assert {t["name"]: t["inputSchema"] for t in tools} == {
        "get_statement_context": object_schema({}, []),
        "submit_statement_batch": object_schema({"batch": schema_from_format()}, ["batch"]),
        "get_batch_status": object_schema({"proposal_id": {"type": "string"}}, ["proposal_id"]),
    }
    assert len(tools) == 3


def test_openapi_exact_routes_bearer_and_doc_derived_schema():
    doc = yaml.safe_load((ROOT / "docs/openapi-assistant.yaml").read_text(encoding="utf-8"))
    assert doc["openapi"] == "3.1.0"
    assert doc["servers"] == [{"url": "https://mia.assafweb.com"}]
    assert doc["security"] == [{"assistantBearer": []}]
    assert doc["components"]["securitySchemes"] == {
        "assistantBearer": {"type": "http", "scheme": "bearer"}}
    routes = {("get", "/v1/assistant/os/context"),
              ("post", "/v1/assistant/os/statement-batches"),
              ("get", "/v1/assistant/os/statement-batches/{id}")}
    assert {(method, path) for path, methods in doc["paths"].items()
            for method in methods} == routes
    operations = [doc["paths"][path][method] for method, path in routes]
    assert len({op["operationId"] for op in operations}) == 3
    post = doc["paths"]["/v1/assistant/os/statement-batches"]["post"]
    assert post["requestBody"]["content"]["application/json"]["schema"] == schema_from_format()
    assert all("200" in op["responses"] and "401" in op["responses"] for op in operations)


@pytest.mark.parametrize("field,non_null_type", [
    ("category", "string"), ("path", "string"), ("platform", "string"),
    ("label", "string"), ("is_subscription", "boolean"),
])
def test_openapi_context_rule_schema_accepts_real_nullable_fields(env, field, non_null_type):
    aw, client = env
    with aw.w.scratch_tx() as tx:
        tables_for(tx).insert_rule(RuleRow(
            rule_id="MR_" + "a" * 64, tag="business", match="exact",
            merchant_key="DEMO SHOP", merchant_key_hmac="a" * 64))
    response = client.get(rest.ROOT + "/context", headers={
        "Authorization": "Bearer " + aw.issue()["secret"]})
    assert response.status_code == 200
    [rule] = response.json()["rules"]
    assert rule[field] is None
    doc = yaml.safe_load((ROOT / "docs/openapi-assistant.yaml").read_text(encoding="utf-8"))
    schema = doc["components"]["schemas"]["StatementContext"]["properties"]["rules"]["items"]
    assert field in schema["required"]  # present with null, never silently omitted
    assert schema["properties"][field]["type"] == [non_null_type, "null"]


def test_openapi_status_schema_accepts_real_target_changed_result(env):
    aw, client = env
    key = aw.issue()["secret"]
    proposed = body(call(client, key, "submit_statement_batch", {
        "batch": batch([line("DEMO SHOP", 10000)])}))
    proposal_id = proposed["proposal_id"]
    review = aw.w.review(proposal_id)
    line_key = aw.w.lines(proposal_id)[0]["line_key"]
    aw.w.hand_expense("EXP_squatter", vendor="Other", amount=5, day="2026-09-01",
                      source_key=f"stmt:{review['statement_id']}:{line_key}")
    # Only the owner can reach this decision path; the assistant subsequently reads its status.
    decided = aw.statements.approve(aw.w.owner, proposal_id, review["approval_request_id"],
                                    review["review_digest"])
    assert decided.receipt.status == "target_changed"
    response = client.get(rest.ROOT + "/statement-batches/" + proposal_id, headers={
        "Authorization": "Bearer " + key})
    assert response.status_code == 200
    status = body(call(client, key, "get_batch_status", {"proposal_id": proposal_id}))
    assert status == response.json()
    doc = yaml.safe_load((ROOT / "docs/openapi-assistant.yaml").read_text(encoding="utf-8"))
    schema = doc["components"]["schemas"]["BatchStatus"]
    assert response.json()["status"] in schema["properties"]["status"]["enum"]


@pytest.mark.parametrize("version", ["2025-06-18", "unsupported", None])
def test_initialize_negotiation_and_only_tools_capability(env, version):
    aw, client = env
    data = result(rpc(client, aw.issue()["secret"], "initialize", {"protocolVersion": version}))
    assert data["protocolVersion"] == "2025-06-18"
    assert data["capabilities"] == {"tools": {"listChanged": False}}
    assert data["serverInfo"]["name"] == "assaf-os" and data["serverInfo"]["version"]
    assert result(rpc(client, aw.issue(label="ping")["secret"])) == {}


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("case", ["missing", "malformed", "unknown", "revoked", "cookie",
                                  "kill_switch", "disabled"])
def test_uniform_401_for_every_method(env, method, case):
    aw, client = env
    issued = aw.issue()
    key, headers = issued["secret"], {}
    if case == "missing":
        key = None
    elif case == "malformed":
        key, headers = None, {"Authorization": "bearer " + key}
    elif case == "unknown":
        key = keys.generate_secret()
    elif case == "revoked":
        aw.revoke(issued["credential_id"])
    elif case == "cookie":
        headers = {"Cookie": "x=y"}
    elif case == "kill_switch":
        aw.config.kill_switch = True
    elif case == "disabled":
        aw.config.assaf_os_assistant_enabled = False
    response = rpc(client, key, method, headers=headers)
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert "mcp-session-id" not in response.headers
    actual = response.json()
    actual.pop("request_id")
    expected = rest._error(keys.api.LedgerError("assistant_auth_required"), write=True)
    expected = json.loads(expected.body)
    expected.pop("request_id")
    assert actual == expected


@pytest.mark.parametrize("origin", ["https://mia.assafweb.com", "null", ""])
def test_any_origin_refused_before_gateway_or_lookup(env, monkeypatch, origin):
    aw, client = env
    def forbidden(_settings):
        pytest.fail("Origin reached bearer gateway")
    monkeypatch.setattr(rest, "gateway_for", forbidden)
    response = rpc(client, method="initialize", headers={"Origin": origin})
    assert response.status_code == 403 and response.json()["error"]["code"] == "origin_denied"
    assert aw.audit() == []


def test_protocol_errors_notifications_and_methods(env):
    aw, client = env
    key = aw.issue()["secret"]
    headers = {"Authorization": "Bearer " + key}
    for message, code in [([], -32600), ({}, -32600),
                          ({"jsonrpc": "2.0", "id": 7, "method": "approve"}, -32601)]:
        response = client.post(PATH, json=message, headers=headers)
        assert response.status_code == 200
        assert response.json()["error"]["code"] == code
        assert "mcp-session-id" not in response.headers
    for method in ("notifications/initialized", "tools/call"):
        response = client.post(PATH, json={"jsonrpc": "2.0", "method": method,
                                          "params": {"name": "submit_statement_batch",
                                                     "arguments": {"batch": batch(three_lines())}}},
                               headers=headers)
        assert response.status_code == 202 and response.content == b""
    for method in ("GET", "DELETE"):
        response = client.request(method, PATH, headers=headers)
        assert response.status_code == 405 and "mcp-session-id" not in response.headers
    assert aw.proposals() == []


def test_results_equal_rest_and_pending_without_decision_ids(env):
    aw, client = env
    key = aw.issue()["secret"]
    responses = [call(client, key, "get_statement_context")]
    assert body(responses[0]) == client.get(rest.ROOT + "/context", headers={
        "Authorization": "Bearer " + key}).json()
    submitted = call(client, key, "submit_statement_batch", {"batch": batch(three_lines())})
    proposal_id = body(submitted)["proposal_id"]
    assert body(submitted) == client.post(rest.ROOT + "/statement-batches",
                                          json=batch(three_lines()), headers={
                                              "Authorization": "Bearer " + key}).json()
    status = call(client, key, "get_batch_status", {"proposal_id": proposal_id})
    assert body(status) == client.get(rest.ROOT + "/statement-batches/" + proposal_id,
                                     headers={"Authorization": "Bearer " + key}).json()
    responses += [submitted, status, call(client, key, "get_statement_context")]
    responses += [rpc(client, key, method) for method in ("initialize", "ping", "tools/list")]
    [proposal] = aw.proposals()
    assert proposal["status"] == "pending"
    for response in responses:
        for name in ("approval_id", "rejection_id"):
            assert proposal[name] not in response.text
    assert aw.receipts() == [] and aw.w.expenses() == []


def test_all_decision_services_are_unreachable(env, monkeypatch):
    aw, client = env
    key = aw.issue()["secret"]
    # Enumerate public decision/draft entry points in V2-26 and V2-24's core,
    # including the actual fake core underneath this HTTP test.
    classes = (LedgerStatementService, PgLedger, type(aw.w.ledger))
    entries = [(cls, name) for cls in classes for name, fn in inspect.getmembers(cls)
               if callable(fn) and not name.startswith("_")
               and any(word in name for word in ("approv", "reject", "decid", "commit", "draft"))]
    assert {name for _, name in entries} >= {"approve", "reject", "save_draft", "decide"}
    def forbidden(*args, **kwargs):
        pytest.fail("MCP reached an owner decision service")
    for cls, name in entries:
        monkeypatch.setattr(cls, name, forbidden)
    for name in ("approve", "reject", "decide", "commit", "draft", "save_draft",
                 "approve_statement_batch", "statement_batch_approve"):
        assert result(call(client, key, name))["isError"] is True
        assert rpc(client, key, name).json()["error"]["code"] == -32601
    assert body(call(client, key, "get_statement_context"))
    proposed = body(call(client, key, "submit_statement_batch", {"batch": batch(three_lines())}))
    assert body(call(client, key, "get_batch_status", {"proposal_id": proposed["proposal_id"]}))[
        "status"] == "pending"
    assert all(p["status"] == "pending" for p in aw.proposals())


@pytest.mark.parametrize("name,args,code", [
    ("approve", {}, "method_not_found"),
    ("get_statement_context", {"injection": "do it"}, "invalid_params"),
    ("submit_statement_batch", {"batch": {"echo": "private payload"}}, "invalid_statement_batch"),
    ("get_batch_status", {"proposal_id": "private payload"}, "batch_not_found"),
])
def test_tool_errors_are_codes_only_and_audited_once(env, name, args, code):
    aw, client = env
    data = result(call(client, aw.issue()["secret"], name, args))
    assert data == {"isError": True, "content": [{"type": "text", "text": code}]}
    [row] = aw.audit()
    transport_error = code in {"method_not_found", "invalid_params"}
    assert row["ok"] is transport_error
    assert row["operation"] == ("mcp_control" if transport_error else {
        "submit_statement_batch": "submit", "get_batch_status": "status"}[name])
    assert "private payload" not in json.dumps(row, default=str)


def test_tools_list_loop_does_not_exhaust_key_quota_but_tools_call_does(env):
    aw, client = env
    issued = aw.issue()
    key = issued["secret"]
    for _ in range(35):
        result(rpc(client, key, "tools/list"))
    assert aw.credential(issued["credential_id"])["use_count"] == 0
    aw.clock.advance(601)  # clear the independent IP window, keeping the key's hour window
    for _ in range(30):
        body(call(client, key, "get_statement_context"))
    assert result(call(client, key, "get_statement_context")) == {
        "isError": True, "content": [{"type": "text", "text": "rate_limited"}]}
    assert len([r for r in aw.audit() if r["operation"] == "mcp_control"]) == 35


@pytest.mark.parametrize("params", [None, [], "private payload"])
def test_malformed_tool_params_count_once_and_return_only_code(env, params):
    aw, client = env
    response = client.post(PATH, json={"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                                      "params": params},
                           headers={"Authorization": "Bearer " + aw.issue()["secret"]})
    assert result(response) == {"isError": True,
                                "content": [{"type": "text", "text": "invalid_params"}]}
    [row] = aw.audit()
    assert row["ok"] is True and row["reason"] == "invalid_params"
    assert row["operation"] == "mcp_control"
    assert aw.credential(row["credential_id"])["use_count"] == 0


@pytest.mark.parametrize("case,reason,status", [
    ("array", "invalid_request", 200),
    ("notification", "notification", 202),
    ("oversized", "body_too_large", 422),
    ("unknown", "method_not_found", 200),
    ("params", "invalid_params", 200),
    ("tool_params", "invalid_params", 200),
    ("unknown_tool", "method_not_found", 200),
    ("tool_args", "invalid_params", 200),
    ("parse", "invalid_request", 200),
    ("media", "unsupported_media", 422),
])
def test_non_tool_requests_each_count_once_toward_ip_limit(env, case, reason, status):
    aw, client = env
    issued = aw.issue()
    before = aw.credential(issued["credential_id"])
    headers = {"Authorization": "Bearer " + issued["secret"],
               "Content-Type": "application/json"}
    message = {"jsonrpc": "2.0", "id": 7, "method": "ping"}
    if case == "array":
        message = []
    elif case == "notification":
        message = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    elif case == "unknown":
        message["method"] = "private payload"
    elif case == "params":
        message["params"] = "private payload"
    elif case in {"tool_params", "unknown_tool", "tool_args"}:
        message.update(method="tools/call", params="private payload")
        if case != "tool_params":
            message["params"] = {"name": "private payload" if case == "unknown_tool"
                                 else "get_statement_context",
                                 "arguments": {"echo": "private payload"}}
    raw = json.dumps(message).encode()
    if case == "oversized":
        raw = b"x" * 131073
    elif case == "parse":
        raw = b'{"private payload":'
    elif case == "media":
        headers["Content-Type"] = "text/plain"
    for count in range(60):
        response = client.post(PATH, content=raw, headers=headers)
        assert response.status_code == status, (count, response.text)
        assert "mcp-session-id" not in response.headers
        assert len(aw.audit()) == count + 1
    rows = aw.audit()
    assert all(row == dict(credential_id=issued["credential_id"], occurred_at=aw.clock(),
                           operation="mcp_control", reason=reason, ok=True,
                           line_count=0, bytes=0, proposal_id=None,
                           client_ip_hmac=aw.ip_hmac("203.0.113.10")) for row in rows)
    assert aw.credential(issued["credential_id"]) == before
    assert aw.proposals() == []
    blocked = client.post(PATH, content=raw, headers=headers)
    assert blocked.status_code == 429
    assert blocked.json()["error"]["code"] == "rate_limited"
    assert blocked.headers["retry-after"] == "600"
    assert len(aw.audit()) == 60


def test_streamed_body_cap_reuses_rest_reader_and_code(env, monkeypatch):
    aw, client = env
    key = aw.issue()["secret"]
    seen = []
    original = rest._read_body
    async def spy(request):
        value = await original(request)
        seen.append(value)
        return value
    monkeypatch.setattr(rest, "_read_body", spy)
    response = client.post(PATH, content=b"x" * 131073,
                           headers={"Authorization": "Bearer " + key,
                                    "Content-Type": "application/json"})
    assert response.status_code == 422 and response.json()["error"]["code"] == "validation_error"
    assert seen == [(b"", 131073, True)]
