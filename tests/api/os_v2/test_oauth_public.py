"""V2-38b public HTTP transport, independent discovery pins and admission boundaries."""

import asyncio
import importlib
import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from app.api.assistant import os as rest
from app.core.config import get_settings
from app.main import app
from app.services import assistant_keys as keys
from app.services.os_ledger import api
from fastapi.testclient import TestClient
from starlette.requests import Request

from tests.services.os_ledger.assistant.support import IP_A, AWorld

BASE = "https://mia.assafweb.com"
RESOURCE = BASE + "/v1/assistant/mcp"
CLIENT = "https://chatgpt.com/oauth/client.json"
REDIRECT = "https://chatgpt.com/connector_platform_oauth_redirect"
GOLDEN = Path(__file__).resolve().parents[2] / "contract/golden/v2-38b"
DISCOVERY = (
    ("/.well-known/oauth-protected-resource", "protected_resource.json"),
    ("/.well-known/oauth-protected-resource/v1/assistant/mcp", "protected_resource.json"),
    ("/.well-known/oauth-authorization-server", "authorization_server.json"),
    ("/.well-known/openid-configuration", "authorization_server.json"),
)


def authorization():
    return {"response_type": "code", "client_id": CLIENT, "redirect_uri": REDIRECT,
            "code_challenge": "a" * 43, "code_challenge_method": "S256",
            "state": "state + / ? & =", "scope": "assaf-os:assistant", "resource": RESOURCE}


def exchange():
    return {"grant_type": "authorization_code", "client_id": CLIENT,
            "redirect_uri": REDIRECT, "code": "code-secret", "code_verifier": "v" * 43,
            "resource": RESOURCE}


@pytest.fixture
def public_env(monkeypatch):
    aw = AWorld("fake")
    aw.config.public_base_url = BASE
    monkeypatch.setattr(rest, "gateway_for", lambda settings: aw.gateway)
    app.dependency_overrides[get_settings] = lambda: aw.config
    with TestClient(app, base_url=BASE, client=(IP_A, 50000),
                    follow_redirects=False) as client:
        yield aw, client
    app.dependency_overrides.pop(get_settings, None)


@pytest.fixture
def transport(public_env, monkeypatch):
    oauth = importlib.import_module("app.api.assistant.oauth")
    service = SimpleNamespace(calls=[], error=None)

    def account(ip, operation):
        service.calls.append(("account", ip, operation))
        if service.error:
            raise service.error

    def authorize(params, *, ip):
        oauth.oauth.validate_authorize(public_env[0].config, params)
        service.calls.append(("authorize", params))
        return SimpleNamespace(oauth_request_id="OR_" + "a" * 32)

    def token(params):
        service.calls.append(("token", params))
        return {"access_token": "aosp_" + "a" * 43, "token_type": "Bearer",
                "expires_in": 2592000, "scope": "assaf-os:assistant"}

    service.account, service.authorize, service.token = account, authorize, token
    monkeypatch.setattr(oauth, "service_for", lambda settings: service)
    return (*public_env, oauth, service)


@pytest.mark.parametrize("path,golden", DISCOVERY)
def test_discovery_exact_document_and_no_host_poisoning(public_env, path, golden):
    _, client = public_env
    response = client.get(path, headers={"Host": "attacker.invalid"})
    assert response.status_code == 200
    assert response.json() == json.loads((GOLDEN / golden).read_text())
    assert "attacker" not in response.text


@pytest.mark.parametrize("setting", ["kill_switch", "assaf_os_assistant_enabled"])
@pytest.mark.parametrize("method,path", [("GET", p) for p, _ in DISCOVERY] + [
    ("GET", "/oauth/authorize"), ("POST", "/oauth/token")])
def test_disabled_public_endpoints_refuse_before_any_db(public_env, monkeypatch, setting,
                                                      method, path):
    aw, client = public_env
    setattr(aw.config, setting, setting == "kill_switch")
    response = client.request(method, path)
    assert response.status_code == 503
    assert "no-store" in response.headers["cache-control"]


def test_authorize_accounts_before_validation_and_redirects_only_to_owner(transport):
    aw, client, _, service = transport
    response = client.get("/oauth/authorize", params=authorization())
    assert response.status_code == 302
    assert response.headers["location"] == aw.config.owner_os_origin + "/#connect/OR_" + "a" * 32
    assert response.headers["cache-control"] == "no-store"
    assert service.calls == [("account", IP_A, "oauth_authorize"),
                             ("authorize", authorization())]


@pytest.mark.parametrize("query", ["state=a&state=b", "bad=%ff", "bad=%", "a" * 4097])
def test_bad_authorize_transport_is_fixed_plain400_counted_without_redirect(transport, query):
    _, client, _, service = transport
    response = client.get("/oauth/authorize?" + query)
    assert response.status_code == 400
    assert response.text == "Invalid authorization request."
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers
    assert service.calls == [("account", IP_A, "oauth_authorize")]


def test_token_form_only_no_store_and_no_secret_in_url(transport):
    _, client, _, service = transport
    response = client.post("/oauth/token", data=exchange())
    assert response.status_code == 200
    assert response.json()["access_token"].startswith("aosp_")
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert "location" not in response.headers
    assert service.calls == [("account", IP_A, "oauth_token"), ("token", exchange())]


@pytest.mark.parametrize("endpoint", ["authorize", "token"])
@pytest.mark.parametrize("suffix", ["", "/", "//", "%2F", "callback"])
def test_slash_paths_never_redirect_or_copy_query(transport, endpoint, suffix):
    _, client, _, service = transport
    method = "GET" if endpoint == "authorize" else "POST"
    response = client.request(method, "/oauth/" + endpoint + "/" + suffix + "?state=private-state")
    assert response.status_code == 400
    assert "location" not in response.headers
    assert "private-state" not in response.text
    assert response.headers["cache-control"] == "no-store"
    assert service.calls == [("account", IP_A, "oauth_" + endpoint)]


@pytest.mark.parametrize("field,value", [
    ("client_id", "https://evil.test/client.json"), ("redirect_uri", REDIRECT + "/"),
    ("response_type", "token"), ("scope", "openid"), ("code_challenge_method", "plain"),
    ("code_challenge", "bad"), ("state", ""), ("state", "unsafe\nstate"),
    ("state", "\u202ehidden"), ("resource", RESOURCE + "/"),
    ("resource", RESOURCE + "?x=y"), ("resource", RESOURCE + "#fragment"),
    ("resource", "https://mia.assafweb.com:443/v1/assistant/mcp"),
    ("resource", "https://user@mia.assafweb.com/v1/assistant/mcp"),
    ("resource", "https://mia.assafweb.com/v1/assistant/%6dcp"),
])
def test_invalid_authorize_semantics_never_redirect_or_create(transport, field, value):
    _, client, _, service = transport
    params = authorization()
    params[field] = value
    response = client.get("/oauth/authorize", params=params)
    assert response.status_code == 400
    assert response.text == "Invalid authorization request."
    assert "location" not in response.headers
    assert service.calls == [("account", IP_A, "oauth_authorize")]


@pytest.mark.parametrize("endpoint", ["authorize", "token"])
def test_unknown_parameters_are_accepted_and_ignored(transport, endpoint):
    _, client, _, service = transport
    params = authorization() if endpoint == "authorize" else exchange()
    extra = {**params, "extra": "unexpected"}
    response = (client.get("/oauth/authorize", params=extra) if endpoint == "authorize"
                else client.post("/oauth/token", data=extra))
    assert response.status_code == (302 if endpoint == "authorize" else 200)
    assert service.calls[-1] == (("authorize", params) if endpoint == "authorize"
                                 else ("token", params))
    assert "unexpected" not in response.text


@pytest.mark.parametrize("endpoint", ["authorize", "token"])
def test_duplicate_unknown_parameter_and_33_fields_are_still_refused(transport, endpoint):
    _, client, _, service = transport
    for raw in ("extra=a&extra=b", "&".join(f"unknown{i}=x" for i in range(33))):
        response = (client.get("/oauth/authorize?" + raw) if endpoint == "authorize"
                    else client.post("/oauth/token", content=raw, headers={
                        "Content-Type": "application/x-www-form-urlencoded"}))
        assert response.status_code == 400
    assert all(call[0] == "account" for call in service.calls)


@pytest.mark.parametrize("base", ["https://api.assafweb.test/app", "https://api.assafweb.test "])
@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer bad"}])
def test_odd_issuer_preserves_uniform_mcp_401_without_resource_metadata(public_env, base, headers):
    aw, client = public_env
    expected = client.post("/v1/assistant/mcp", headers=headers, json={}).json()
    aw.config.public_base_url = base
    response = client.post("/v1/assistant/mcp", headers=headers, json={})
    assert response.status_code == 401
    body = response.json()
    assert re.fullmatch(r"[0-9a-f]{32}", body.pop("request_id"))
    expected.pop("request_id")
    assert body == expected
    # Brief correction: preserve V2-37/V2-38's original plain Bearer challenge.
    assert response.headers["www-authenticate"] == "Bearer"
    assert "resource_metadata" not in response.headers["www-authenticate"]


@pytest.mark.parametrize("code", ["invalid_request", "invalid_grant", "invalid_client",
                                 "invalid_scope", "unsupported_grant_type"])
def test_token_service_errors_are_oauth_json_without_details(transport, monkeypatch, code):
    _, client, oauth, service = transport

    def fail(params):
        raise oauth.oauth.OAuthError(code)

    monkeypatch.setattr(service, "token", fail)
    response = client.post("/oauth/token", data=exchange())
    assert response.status_code == 400
    assert response.json() == {"error": code}
    assert response.headers["pragma"] == "no-cache"


@pytest.mark.parametrize("endpoint", ["authorize", "token"])
def test_busy_guard_has_no_db_effect(transport, monkeypatch, endpoint):
    _, client, _, service = transport
    slots = threading.BoundedSemaphore(1)
    assert slots.acquire(blocking=False)
    monkeypatch.setattr(keys, "_bearer_slots", slots)
    monkeypatch.setattr(keys, "_BEARER_WAIT_SECONDS", 0.02)
    try:
        response = (client.get("/oauth/authorize", params=authorization())
                    if endpoint == "authorize" else client.post("/oauth/token", data=exchange()))
    finally:
        slots.release()
    assert response.status_code == 503
    assert response.json() == {"error": "temporarily_unavailable"}
    assert service.calls == []


@pytest.mark.parametrize("endpoint", ["authorize", "token"])
@pytest.mark.parametrize("invalid", [False, True])
def test_single_guard_covers_account_and_work_and_releases(transport, monkeypatch,
                                                         endpoint, invalid):
    _, client, _, service = transport

    class Slots:
        def __init__(self):
            self.acquisitions = 0
            self.held = False

        def acquire(self, **kwargs):
            self.acquisitions += 1
            assert not self.held
            self.held = True
            return True

        def release(self):
            assert self.held
            self.held = False

    slots = Slots()
    monkeypatch.setattr(keys, "_bearer_slots", slots)

    def observe(original):
        def observed(*args, **kwargs):
            assert slots.held
            return original(*args, **kwargs)
        return observed

    for name in ("account", endpoint):
        monkeypatch.setattr(service, name, observe(getattr(service, name)))
    if endpoint == "authorize":
        response = client.get("/oauth/authorize", params="bad=%ff" if invalid else authorization())
    else:
        response = client.post(
            "/oauth/token", content="bad=%ff" if invalid else urlencode(exchange()),
            headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert response.status_code == (400 if invalid else 302 if endpoint == "authorize" else 200)
    assert slots.acquisitions == 1
    assert not slots.held


@pytest.mark.asyncio
async def test_streamed_token_body_stops_at_cap_without_reading_tail(transport):
    aw, _, oauth, service = transport
    chunks = iter([b"x" * 2048, b"x" * 2049])
    reads = []

    async def receive():
        reads.append(True)
        return {"type": "http.request", "body": next(chunks), "more_body": True}

    request = Request({"type": "http", "method": "POST", "path": "/oauth/token",
                       "client": (IP_A, 50000), "headers": [
                           (b"content-type", b"application/x-www-form-urlencoded")]},
                      receive=receive)
    response = await oauth.token(request, aw.config)
    assert response.status_code == 400
    assert json.loads(response.body) == {"error": "invalid_request"}
    assert len(reads) == 2
    assert service.calls == [("account", IP_A, "oauth_token")]


@pytest.mark.parametrize("body,content_type", [
    ("code=a&code=b", "application/x-www-form-urlencoded"),
    ("code=%ff", "application/x-www-form-urlencoded"),
    ("code=%", "application/x-www-form-urlencoded"),
    ("a" * 4097, "application/x-www-form-urlencoded"),
    ('{"code":"a"}', "application/json"),
])
def test_bad_token_transport_counted_once(transport, body, content_type):
    _, client, _, service = transport
    response = client.post("/oauth/token", content=body, headers={"Content-Type": content_type})
    assert response.status_code == 400
    assert response.json() == {"error": "invalid_request"}
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["pragma"] == "no-cache"
    assert service.calls == [("account", IP_A, "oauth_token")]


@pytest.mark.parametrize("endpoint", ["authorize", "token"])
def test_ip_limit_precedes_parsing(transport, endpoint):
    _, client, _, service = transport
    service.error = api.LedgerError("rate_limited", reason="ip_limit")
    response = (client.get("/oauth/authorize?bad=%ff") if endpoint == "authorize" else
                client.post("/oauth/token", content="bad=%ff"))
    assert response.status_code == 429
    assert response.headers["retry-after"] == "600"
    assert service.calls == [("account", IP_A, "oauth_" + endpoint)]


@pytest.mark.parametrize("headers,invalid", [({}, False),
    ({"Authorization": "Bearer bad"}, True), ({"Authorization": ""}, True),
    ({"Cookie": "session=x"}, False)])
def test_mcp_401_challenge_preserves_uniform_envelope(public_env, headers, invalid):
    _, client = public_env
    response = client.post("/v1/assistant/mcp", headers=headers, json={})
    assert response.status_code == 401
    expected = 'Bearer resource_metadata="' + BASE + (
        '/.well-known/oauth-protected-resource/v1/assistant/mcp"')
    if invalid:
        expected += ', error="invalid_token"'
    assert response.headers["www-authenticate"] == expected
    assert response.json()["error"] == {"code": "assistant_auth_required", "retryable": False,
                                        "write_outcome": "not_started"}


def test_mcp_origin_still_precedes_auth(public_env):
    _, client = public_env
    response = client.post("/v1/assistant/mcp", headers={"Origin": "https://bad.test"}, json={})
    assert response.status_code == 403
    assert "www-authenticate" not in response.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("endpoint", ["authorize", "token"])
@pytest.mark.parametrize("stage", ["account", "work"])
async def test_cancelled_oauth_holds_single_slot_until_db_finishes(transport, monkeypatch,
                                                                endpoint, stage):
    aw, _, oauth, service = transport
    entered, release = threading.Event(), threading.Event()
    slots = threading.BoundedSemaphore(1)
    monkeypatch.setattr(keys, "_bearer_slots", slots)
    original = getattr(service, "account" if stage == "account" else endpoint)

    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "account" if stage == "account" else endpoint, blocked)
    body = urlencode(exchange()).encode()

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    request = Request({"type": "http", "method": "GET" if endpoint == "authorize" else "POST",
                       "path": "/oauth/" + endpoint, "client": (IP_A, 50000),
                       "query_string": urlencode(authorization()).encode(),
                       "headers": [(b"content-type", b"application/x-www-form-urlencoded")]},
                      receive=receive)
    task = asyncio.create_task(getattr(oauth, endpoint)(request, aw.config))
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        task.cancel()
        await asyncio.sleep(0.05)
        assert not slots.acquire(blocking=False)
        assert not task.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert slots.acquire(blocking=False)
        slots.release()
    finally:
        release.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
