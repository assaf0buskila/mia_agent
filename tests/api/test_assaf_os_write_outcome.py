"""V2-05: only proven non-starts may release an owner's pending write marker."""

import asyncio
import json
import logging
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest
from app.api import assaf_os as api
from app.api.owner_os_boundary import OwnerOSBoundary, error_response
from app.services import assaf_os as port

from tests.unit import test_assaf_os as assaf
from tests.unit import test_owner_sessions as auth

sessions = auth.sessions
OPS = ("propose", "approve", "reject")
ROOT = "/v1/owner/os/unified"
SENTINEL = "PRIVATE payload must never be logged"
ACTUAL_POST = port._post


def path(op, proposal_id=assaf.PROPOSAL):
    return ROOT + "/proposals" + ("" if op == "propose" else f"/{proposal_id}/{op}")


def body(op, config):
    pending = assaf._pending(config, "OS")
    if op == "propose":
        return {key: pending[key] for key in (
            "request_id", "entity", "record_id", "expected_revision", "input"
        )}
    key = "approval_request_id" if op == "approve" else "rejection_request_id"
    return {"request_id": pending[key]}


@pytest.fixture
def setup(sessions, monkeypatch):
    config = assaf.settings()
    port._clear_assaf_os_decision_binding_cache()
    port._clear_assaf_os_schema_cache()
    calls = []

    def success(_url, raw, _budget):
        request = json.loads(json.loads(raw)["payload"])
        op = request["operation"]
        calls.append(op)
        if op in {"propose", "proposal"}:
            result = assaf._pending(config, "OS")
        elif op == "snapshot":
            result = assaf._snapshot(config)
        else:
            result = assaf._decision_result(
                config, request, status="rejected" if op == "reject" else "committed"
            )
        return assaf.signed(raw, result, config)

    monkeypatch.setattr(port, "_post", success)
    with assaf.client(sessions, config) as browser:
        login = auth._login(browser)
        headers = {"Origin": auth.ORIGIN, "X-CSRF-Token": login["csrf_token"]}
        yield config, browser, headers, calls
    port._clear_assaf_os_decision_binding_cache()
    port._clear_assaf_os_schema_cache()


def assert_error(response, status, code, *, not_started, op, caplog):
    assert response.status_code == status, response.text
    envelope = response.json()
    assert set(envelope) == {"api_version", "request_id", "error"}
    expected = {"code": code, "retryable": status in {429, 503, 504}}
    if not_started:
        expected["write_outcome"] = "not_started"
    assert envelope["error"] == expected
    lines = [r.getMessage() for r in caplog.records
             if r.getMessage().startswith("owner_os_write ")]
    outcome = "not_started" if not_started else "unknown"
    assert lines == [f"owner_os_write op={op} outcome={outcome} reason={code}"]
    assert SENTINEL not in " ".join(lines)


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("case,status", [
    ("size", 413), ("type", 415), ("json", 422), ("duplicate", 422),
    ("array", 422), ("body", 422), ("empty", 422),
])
def test_boundary_and_fastapi_validation(setup, op, case, status, caplog):
    config, browser, headers, calls = setup
    raw = json.dumps(body(op, config))
    headers = {**headers, "Content-Type": "application/json"}
    if case == "size":
        raw = "x" * 16_385
    elif case == "type":
        headers["Content-Type"] = "text/plain"
    elif case == "json":
        raw = "{"
    elif case == "duplicate":
        raw = '{"request_id":1,"request_id":2}'
    elif case == "array":
        raw = "[]"
    elif case == "body":
        raw = json.dumps({**body(op, config), "unexpected": SENTINEL})
    elif case == "empty":
        raw = ""
    with caplog.at_level(logging.INFO):
        response = browser.post(path(op), headers=headers, content=raw)
    assert calls == []
    assert_error(response, status, "validation_error", not_started=True, op=op, caplog=caplog)


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("case,status,code", [
    ("origin", 403, "origin_denied"), ("session", 401, "owner_auth_required"),
    ("scope", 403, "owner_scope_denied"), ("csrf", 403, "csrf_denied"),
    ("kill", 403, "kill_switch"), ("port_disabled", 404, "operation_unavailable"),
    ("port_config", 503, "operation_unavailable"),
    ("call_kill", 403, "kill_switch"), ("call_size", 422, "validation_error"),
])
def test_predispatch_guards(setup, monkeypatch, op, case, status, code, caplog):
    config, browser, headers, calls = setup
    if case == "origin":
        headers = {**headers, "Origin": "https://denied.invalid"}
    elif case == "session":
        browser.cookies.clear()
    elif case == "scope":
        config.owner_os_session_scopes = "assaf-os:read"
    elif case == "csrf":
        headers = {**headers, "X-CSRF-Token": "wrong"}
    elif case == "kill":
        config.kill_switch = True
    elif case.startswith("port_"):
        original = api._port

        def unavailable(settings, owner):
            # Change at construction, after the real auth/scope checks.
            if case == "port_disabled":
                settings.assaf_os_enabled = False
            else:
                settings.assaf_os_script_url = "https://wrong.invalid"
            return original(settings, owner)

        monkeypatch.setattr(api, "_port", unavailable)
    elif case == "call_size":
        monkeypatch.setattr(port, "MAX_BODY_BYTES", 1)
    elif case == "call_kill":
        original = port.AssafOsPort._call

        def switch(self, operation, data, **kwargs):
            self.settings.kill_switch = True
            return original(self, operation, data, **kwargs)

        monkeypatch.setattr(port.AssafOsPort, "_call", switch)
    # A cached binding reaches _call's own guards without a read first.
    port._remember_decision_binding(config, auth.OWNER_ID, assaf._pending(config, "OS"))
    with caplog.at_level(logging.INFO):
        response = browser.post(path(op), headers=headers, json=body(op, config))
    assert calls == []
    assert_error(response, status, code, not_started=True, op=op, caplog=caplog)


@pytest.mark.parametrize("op", ("approve", "reject"))
@pytest.mark.parametrize("case,code,status", [
    ("id", "invalid_proposal_id", 422), ("forbidden", "forbidden", 403),
    ("binding", "decision_request_mismatch", 409),
])
def test_decision_only_guards(setup, monkeypatch, op, case, code, status, caplog):
    config, browser, headers, calls = setup
    request_body = body(op, config)
    if case == "binding":
        request_body["request_id"] = assaf.REQUEST
    elif case == "forbidden":
        original = port.AssafOsPort._call

        def wrong_actor(self, operation, data, **kwargs):
            if operation in {"approve", "reject"}:
                self.actor_source = "MIA"
            return original(self, operation, data, **kwargs)

        monkeypatch.setattr(port.AssafOsPort, "_call", wrong_actor)
    with caplog.at_level(logging.INFO):
        response = browser.post(path(op, "1" if case == "id" else assaf.PROPOSAL),
                                headers=headers, json=request_body)
    assert calls == ([] if case == "id" else ["proposal"])
    assert_error(response, status, code, not_started=True, op=op, caplog=caplog)


FAULTS = (
    "signed_busy", "unsigned_busy", "bare_busy", "parse", "identity", "result",
    "timeout", "transport", "non200", "wrong_host", "unparseable_302",
    "busy_after_open", "signed_other", "signed_invalid", "busy_bad_shape",
    "busy_ok_true",
)


def fault_transport(monkeypatch, config, fault, calls):
    """Exercise real _post, including redirect handling, without external I/O."""
    wire_methods = []

    class Response(BytesIO):
        status = 200
        headers = {"Content-Type": "application/json"}

    class Opener:
        def open(self, request, **_kwargs):
            method = request.get_method()
            wire_methods.append(method)
            if fault == "timeout":
                raise TimeoutError(SENTINEL)
            if fault == "transport":
                raise URLError(SENTINEL)
            if fault in {"wrong_host", "unparseable_302"} and method == "POST":
                host = "wrong.invalid" if fault == "wrong_host" else "script.googleusercontent.com"
                raise HTTPError(request.full_url, 302, "Found",
                                {"Location": f"https://{host}/result"}, BytesIO())
            if fault in {"parse", "unparseable_302"}:
                return Response(b"<html>unparseable</html>")
            if fault == "busy_after_open":
                # The post-open busy site is outside doPost (pinned by the node test).
                # A transport exception with that code is never a signed busy proof.
                raise port.AssafOsError("runtime_busy")
            if fault == "bare_busy":
                return Response(b'{"ok":false,"data":{"error":{"code":"runtime_busy"}}}')
            code = "target_changed" if fault == "signed_other" else (
                "invalid_input" if fault == "signed_invalid" else "runtime_busy"
            )

            def mutate(envelope):
                envelope.update(ok=False, data={"error": {"code": code}})
                if fault == "identity":
                    envelope["request_id"] = "wrong-request-id"
                elif fault == "result":
                    envelope.update(ok=True, data={"private": SENTINEL})
                elif fault == "busy_bad_shape":
                    envelope["data"]["extra"] = True
                elif fault == "busy_ok_true":
                    envelope["ok"] = True

            result = Response(assaf.signed(request.data, {}, config, mutate=mutate,
                                          valid_signature=fault != "unsigned_busy"))
            if fault == "non200":
                result.status = 500
            return result

    monkeypatch.setattr(port, "build_opener", lambda *_: Opener())

    def counted(url, raw, budget):
        calls.append(json.loads(json.loads(raw)["payload"])["operation"])
        return ACTUAL_POST(url, raw, budget)

    monkeypatch.setattr(port, "_post", counted)
    return wire_methods


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("fault", FAULTS)
def test_after_dispatch_only_signed_busy_is_not_started(
    setup, monkeypatch, op, fault, caplog
):
    config, browser, headers, calls = setup
    # setup's successful stub is replaced by the actual transport under a fake opener.
    port._remember_decision_binding(config, auth.OWNER_ID, assaf._pending(config, "OS"))
    wire = fault_transport(monkeypatch, config, fault, calls)
    with caplog.at_level(logging.INFO):
        response = browser.post(path(op), headers=headers, json=body(op, config))
    code = {"signed_busy": "runtime_busy", "signed_other": "target_changed",
            "signed_invalid": "invalid_input"}.get(fault, "unknown")
    status = {"signed_other": 409, "signed_invalid": 422}.get(fault, 503)
    assert calls == [op], "the write _post is never retried"
    assert wire == (["POST", "GET"] if fault == "unparseable_302" else ["POST"])
    assert_error(response, status, code, not_started=fault == "signed_busy", op=op, caplog=caplog)


@pytest.mark.parametrize("op", ("approve", "reject"))
@pytest.mark.parametrize("fault", FAULTS)
def test_any_preread_error_including_timeout_is_not_started(
    setup, monkeypatch, op, fault, caplog
):
    config, browser, headers, calls = setup
    wire = fault_transport(monkeypatch, config, fault, calls)
    with caplog.at_level(logging.INFO):
        response = browser.post(path(op), headers=headers, json=body(op, config))
    code = {"signed_busy": "runtime_busy", "signed_other": "target_changed",
            "signed_invalid": "invalid_input"}.get(fault, "source_unavailable")
    # A raw AssafOsError in a read is propagated, but still precedes any write.
    if fault == "busy_after_open":
        code = "runtime_busy"
    status = {"signed_other": 409, "signed_invalid": 422}.get(fault, 503)
    assert calls == ["proposal"]
    assert wire.count("POST") == 1, "only the read's transport POST; no write or retry"
    assert_error(response, status, code, not_started=True, op=op, caplog=caplog)


@pytest.mark.parametrize("op", OPS)
def test_success_never_has_key_and_cold_decisions_do_not_repeat_writes(setup, op, caplog):
    config, browser, headers, calls = setup
    with caplog.at_level(logging.INFO):
        response = browser.post(path(op), headers=headers, json=body(op, config))
    assert response.status_code == 200, response.text
    assert "write_outcome" not in response.text
    assert calls == (["propose"] if op == "propose" else ["proposal", op])
    assert not any(r.getMessage().startswith("owner_os_write ") for r in caplog.records)


@pytest.mark.parametrize("fault", (None, "signed_busy", "timeout"))
def test_get_never_has_key(setup, monkeypatch, fault, caplog):
    config, browser, headers, calls = setup
    if fault:
        fault_transport(monkeypatch, config, fault, calls)
    with caplog.at_level(logging.INFO):
        response = browser.get(ROOT, headers=headers)
    assert response.status_code == (503 if fault else 200)
    assert "write_outcome" not in response.text
    assert calls == ["snapshot"]
    assert not any(r.getMessage().startswith("owner_os_write ") for r in caplog.records)


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("after_dispatch", (False, True))
def test_catch_all_is_always_unknown(setup, monkeypatch, op, after_dispatch, caplog):
    config, browser, headers, calls = setup
    port._remember_decision_binding(config, auth.OWNER_ID, assaf._pending(config, "OS"))

    def crash(*args):
        if after_dispatch:
            calls.append(op)
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(port if after_dispatch else api,
                        "_post" if after_dispatch else "_port", crash)
    with caplog.at_level(logging.INFO):
        response = browser.post(path(op), headers=headers, json=body(op, config))
    assert calls == ([op] if after_dispatch else [])
    assert_error(response, 503, "source_unavailable", not_started=False, op=op, caplog=caplog)


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("key", (
    "api_version", "operation", "actor_source", "owner_id", "workspace_id",
    "workbook_id", "request_id", "timestamp", "mode",
))
def test_signed_busy_must_echo_every_identity_field(setup, monkeypatch, op, key, caplog):
    config, browser, headers, calls = setup
    port._remember_decision_binding(config, auth.OWNER_ID, assaf._pending(config, "OS"))

    def mismatch(_url, raw, _budget):
        calls.append(op)

        def mutate(envelope):
            envelope.update(ok=False, data={"error": {"code": "runtime_busy"}})
            envelope[key] = 0 if key == "timestamp" else "different"

        return assaf.signed(raw, {}, config, mutate=mutate)

    monkeypatch.setattr(port, "_post", mismatch)
    with caplog.at_level(logging.INFO):
        response = browser.post(path(op), headers=headers, json=body(op, config))
    assert calls == [op]
    assert_error(response, 503, "unknown", not_started=False, op=op, caplog=caplog)


@pytest.mark.parametrize("method,url", [
    ("GET", path("propose")), ("PUT", path("approve")),
    ("POST", ROOT + "/proposals/extra"), ("POST", path("approve") + "/extra"),
    ("POST", "/v1/owner/auth/login"), ("POST", "/v1/owner/os/other"),
])
def test_only_the_three_exact_post_routes_open_dispatch(method, url):
    from app.core.owner_write import current_write_dispatch

    async def run():
        messages = []

        async def app(scope, receive, send):
            assert current_write_dispatch() is None
            await error_response(503, "source_unavailable")(scope, receive, send)

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        await OwnerOSBoundary(app)({"type": "http", "path": url, "method": method,
                                    "headers": [], "query_string": b""}, receive, send)
        assert "write_outcome" not in json.loads(messages[-1]["body"])["error"]
        assert current_write_dispatch() is None

    asyncio.run(run())


def test_dispatch_counter_and_request_isolation():
    # Imported here so the red run also executes the HTTP assertions before implementation.
    from app.core.owner_write import current_write_dispatch

    async def run():
        states = []
        reached = asyncio.Event()

        async def app(scope, receive, send):
            state = current_write_dispatch()
            if scope["method"] == "GET":
                assert state is None
                return await error_response(503, "runtime_busy")(scope, receive, send)
            assert state is not None
            assert (state.dispatched, state.signed_busy) == (0, False)
            states.append(state)
            state.dispatched = 2
            state.signed_busy = True
            if len(states) == 2:
                reached.set()
            await reached.wait()
            return await error_response(503, "runtime_busy")(scope, receive, send)

        boundary = OwnerOSBoundary(app)

        async def request(method):
            messages = []

            async def receive():
                return {"type": "http.request", "body": b"", "more_body": False}

            async def send(message):
                messages.append(message)

            await boundary({"type": "http", "path": path("propose"), "method": method,
                            "headers": [], "query_string": b""}, receive, send)
            assert "write_outcome" not in json.loads(messages[-1]["body"])["error"]
            assert current_write_dispatch() is None

        await asyncio.gather(request("POST"), request("POST"), request("GET"))
        assert states[0] is not states[1]

    asyncio.run(run())
