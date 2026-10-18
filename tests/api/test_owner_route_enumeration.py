"""V2-19 (V2_PG_CONTRACTS.md §9, I4, §20, §22 E19): the owner boundary, enumerated.

Walks every route ``app.main`` mounts (through FastAPI's included-router wrappers, which is why
``app.router.routes`` alone proves nothing, E19) and every Mia owner tool, then pins:
  * the route table is fully classified, so a new route cannot slip in unclassified;
  * every owner cookie-session route answers an ``Authorization`` header with 401, even when
    the request also carries a valid session, the exact Origin and CSRF (I4);
  * an owner write without the exact Origin, or without CSRF, is refused by the auth layer, before
    any handler runs (no write);
  * only ``owner_mutation_scope`` sets the mutation proof that ``from_owner_mutation`` needs;
  * ``assaf-os:direct`` is a known cookie-session write scope, and Google login keeps it;
  * commit/undo/abandon are unreachable from MIA, Telegram and bearer.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from app.api import owner_sessions
from app.api.os_v2.errors import ledger_error_handler
from app.api.owner_sessions import (
    ACTION_SCOPES,
    ASSAF_OS_WRITE_SCOPES,
    CSRF_HEADER,
    KNOWN_SCOPES,
    OwnerSessionPrincipal,
    owner_mutation_scope,
    owner_scope,
)
from app.capabilities.types import Principal
from app.core.config import get_settings
from app.main import app
from app.services.os_ledger import api, authorize
from app.services.owner_sessions import login_proof_hash
from app.tools.registries import owner_tools
from fastapi import Depends, FastAPI
from fastapi.routing import APIRoute, _IncludedRouter
from fastapi.testclient import TestClient

from tests.api.os_v2.v2_19_support import ORIGIN, OwnerApp, owner_settings, session_factory
from tests.fakes.contexts import make_ctx as fake_ctx
from tests.fakes.os_ledger_env import OWNER_ID

SAMPLE_ID = "12345678-1234-4234-8234-123456789012"
SESSION_DEPENDENCIES = {
    "app.api.owner_sessions.owner_scope.<locals>.dependency",
    "app.api.owner_sessions.owner_mutation_scope.<locals>.dependency",
}
# Routes on /v1/owner that are not cookie-session routes. They authenticate with the legacy
# Telegram Login Widget proof header (X-AssafWeb-Owner-Login), never reach the ledger, and are
# outside V2-19's files; pinned here so a new one cannot appear silently.
TELEGRAM_PROOF_ROUTES = {
    ("POST", "/v1/owner/quick-leads/proposals"),
    ("POST", "/v1/owner/quick-leads/approvals/{approval_id}"),
}
GOOGLE = dict(owner_os_google_enabled=True, owner_os_google_client_id="client.example",
              owner_os_google_subject="109876543210", owner_os_google_owner_id=OWNER_ID)


def mounted() -> list[APIRoute]:
    def walk(routes, prefix=""):
        for route in routes:
            if isinstance(route, _IncludedRouter):
                yield from walk(route.original_router.routes,
                                prefix + (route.include_context.prefix or ""))
            elif isinstance(route, APIRoute):
                assert prefix == "", "an include prefix would change the route's real path"
                yield route

    return list(walk(app.router.routes))


def dependency_names(route: APIRoute) -> set[str]:
    names: set[str] = set()

    def walk(dependant):
        for child in dependant.dependencies:
            call = child.call
            names.add(f"{getattr(call, '__module__', '')}.{getattr(call, '__qualname__', '')}")
            walk(child)

    walk(route.dependant)
    return names


def classify(route: APIRoute) -> str:
    module = route.endpoint.__module__
    if module == "app.api.owner_sessions":
        return "session_auth"  # challenge/login/session/logout/revoke-all/refresh
    if module.startswith("app.api.os_v2.") or module == "app.api.owner_step_up":
        return "owner_session"
    if dependency_names(route) & SESSION_DEPENDENCIES:
        return "owner_session"
    if module == "app.api.quick_leads":
        return "telegram_login_proof"
    if module.startswith("app.api.assistant."):
        return "bearer"
    return {
        "app.api.website": "public_website",
        "app.api.telegram": "telegram_webhook",
        "app.api.form_intake": "internal_form",
        "app.main": "health",
    }[module]


def cells() -> list[tuple[str, str, str]]:
    return sorted((method, route.path, classify(route))
                  for route in mounted() for method in route.methods)


def concrete(path: str) -> str:
    return re.sub(r"\{([^}:]+)(?::[^}]+)?\}",
                  lambda m: SAMPLE_ID if m.group(1).endswith("request_id") else "x1", path)


def owner_cells(*classes: str) -> list[tuple[str, str]]:
    return [(method, path) for method, path, kind in cells() if kind in classes]


def test_the_walk_sees_exactly_what_the_app_serves():
    walked = {(method, route.path) for route in mounted() for method in route.methods
              if route.include_in_schema}
    schema = {(method.upper(), path) for path, operations in app.openapi()["paths"].items()
              for method in operations}
    assert walked == schema


def test_every_route_is_classified_and_every_owner_path_is_a_known_kind():
    table = cells()  # classify() raises KeyError on an unknown module
    assert table
    for method, path, kind in table:
        if path.startswith("/v1/owner"):
            assert kind in {"owner_session", "session_auth", "telegram_login_proof"}, (method, path)
        if kind == "bearer":
            assert not path.startswith("/v1/owner"), path
    assert {(m, p) for m, p, k in table if k == "telegram_login_proof"} == TELEGRAM_PROOF_ROUTES
    assert {p for _, p, k in table if k == "session_auth"} >= {
        "/v1/owner/auth/challenge", "/v1/owner/auth/login", "/v1/owner/auth/google/login",
        "/v1/owner/auth/session", "/v1/owner/auth/logout", "/v1/owner/auth/revoke-all",
        "/v1/owner/auth/refresh"}
    assert ("GET", "/v1/owner/os/unified/pending", "owner_session") in table
    assert ("GET", "/v1/owner/os/unified/proposal-requests/{request_id}", "owner_session") in table


def request(owner: OwnerApp, method: str, path: str, headers: dict[str, str]):
    owner.client.cookies.clear()
    body = {} if method != "GET" else None
    return owner.client.request(method, concrete(path), headers=headers, json=body)


@pytest.mark.parametrize("value", ["Bearer aosp_" + "a" * 43, "Basic eDp5"])
def test_every_owner_route_refuses_an_authorization_header_with_401(value):
    with OwnerApp(owner_settings(**GOOGLE)) as owner:
        targets = owner_cells("owner_session", "session_auth")
        assert len(targets) >= 30
        for method, path in targets:
            headers = owner.login() | {"Authorization": value}
            response = request(owner, method, path, headers)
            assert response.status_code == 401, (method, path, response.text)
            assert response.json()["error"]["code"] == "owner_auth_required", (method, path)


def test_an_owner_write_without_the_exact_origin_or_csrf_never_reaches_its_handler():
    # Every scope granted, so the refusal can only come from the Origin or the CSRF check.
    every = ",".join(sorted(KNOWN_SCOPES))
    settings = owner_settings(owner_os_session_scopes=every, business_ledger_test_enabled=True)
    with OwnerApp(settings) as owner:
        writes = [(m, p) for m, p in owner_cells("owner_session") if m != "GET"]
        assert len(writes) >= 10
        for method, path in writes:
            no_origin = owner.login(scopes=KNOWN_SCOPES)
            del no_origin["Origin"]
            for headers in (no_origin, no_origin | {"Origin": "null"},
                            no_origin | {"Origin": "https://evil.example"}):
                response = request(owner, method, path, headers)
                assert response.status_code == 403, (method, path, response.text)
                assert response.json()["error"]["code"] == "origin_denied"
            for csrf in (None, "", "f" * 64):
                headers = owner.login(scopes=KNOWN_SCOPES)
                if csrf is None:
                    del headers[CSRF_HEADER]
                else:
                    headers[CSRF_HEADER] = csrf
                response = request(owner, method, path, headers)
                assert response.status_code == 403, (method, path, response.text)
                assert response.json()["error"]["code"] == "csrf_denied"


# ---------------------------------------------------------------------------------------------
# The browser mutation proof (§9): set only by owner_mutation_scope, after Origin + CSRF pass.


def proof_app(settings) -> FastAPI:
    probe = FastAPI()
    probe.include_router(owner_sessions.router)
    probe.add_exception_handler(api.LedgerError, ledger_error_handler)

    def report(principal: OwnerSessionPrincipal, build) -> dict:
        ctx = build(principal, settings)
        return {"proof": owner_sessions._mutation_proven(principal),
                "verified": ctx.browser_mutation_verified, "channel": ctx.channel}

    @probe.get("/read")
    def read(principal=Depends(owner_scope("assaf-os:read"))):
        return report(principal, authorize.from_owner_read)

    @probe.get("/read-as-write")
    def read_as_write(principal=Depends(owner_scope("assaf-os:direct"))):
        return report(principal, authorize.from_owner_mutation)

    @probe.post("/write")
    def write(principal=Depends(owner_mutation_scope("assaf-os:direct"))):
        return report(principal, authorize.from_owner_mutation)

    return probe


def test_only_owner_mutation_scope_sets_the_proof_from_owner_mutation_needs():
    settings = owner_settings()
    factory = session_factory()
    probe = proof_app(settings)

    def db():
        with factory() as session:
            yield session

    probe.dependency_overrides[owner_sessions.get_owner_session_db] = db
    probe.dependency_overrides[get_settings] = lambda: settings
    bound = OwnerApp(settings)
    bound.factory = factory
    client = TestClient(probe, base_url=ORIGIN)
    headers = bound.login()
    assert client.get("/read", headers=headers).json() == {
        "proof": False, "verified": False, "channel": "OS"}
    refused = client.get("/read-as-write", headers=headers)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "browser_mutation_verification_required"
    assert client.post("/write", headers=headers).json() == {
        "proof": True, "verified": True, "channel": "OS"}
    no_csrf = dict(headers)
    del no_csrf[CSRF_HEADER]
    assert client.post("/write", headers=no_csrf).status_code == 403


# ---------------------------------------------------------------------------------------------
# The scope set (§9)


def test_assaf_os_direct_is_a_known_cookie_session_write_scope():
    assert ASSAF_OS_WRITE_SCOPES == {"assaf-os:propose", "assaf-os:approve", "assaf-os:direct"}
    assert KNOWN_SCOPES == {
        "owner-os:read:contacts", "owner-os:read:activities", "owner-os:read:reminders",
        "quick_leads.create", "reminders.write", "business-ledger:read", "business-ledger:write",
        "assaf-os:read", "assaf-os:propose", "assaf-os:approve", "assaf-os:direct",
    }
    owner_scope("assaf-os:direct")
    owner_mutation_scope("assaf-os:direct")
    with pytest.raises(ValueError):
        owner_mutation_scope("assaf-os:read")


def test_the_configured_scopes_gate_direct_with_the_other_assaf_os_scopes():
    on = owner_sessions._configured_scopes(owner_settings())
    off = owner_sessions._configured_scopes(owner_settings(assaf_os_enabled=False))
    assert "assaf-os:direct" in on
    assert not {s for s in off if s.startswith("assaf-os:")}


def test_google_login_keeps_the_configured_scope_set_exactly(monkeypatch):
    configured = ("owner-os:read:contacts,quick_leads.create,reminders.write,assaf-os:read,"
                  "assaf-os:propose,assaf-os:approve,assaf-os:direct")
    settings = owner_settings(**GOOGLE, owner_os_session_scopes=configured,
                              telegram_bot_token="")

    def verified(*_args, **_kwargs):
        return SimpleNamespace(owner_id=OWNER_ID,
                               proof_hash=login_proof_hash(secrets.token_hex(32)),
                               authenticated_at=datetime.now(UTC) - timedelta(seconds=1))

    monkeypatch.setattr(owner_sessions, "verify_google_owner_token", verified)
    with OwnerApp(settings) as owner:
        challenge = owner.client.get("/v1/owner/auth/challenge", headers={"Origin": ORIGIN})
        assert challenge.status_code == 200, challenge.text
        csrf = challenge.json()["csrf_token"]
        login = owner.client.post("/v1/owner/auth/google/login",
                                  headers={"Origin": ORIGIN, CSRF_HEADER: csrf},
                                  json={"id_token": "synthetic"})
        assert login.status_code == 200, login.text
        expected = set(configured.split(",")) - ACTION_SCOPES
        assert set(login.json()["scopes"]) == expected
        assert "assaf-os:direct" in expected
        assert owner_sessions._configured_scopes(settings) - ACTION_SCOPES == expected


# ---------------------------------------------------------------------------------------------
# commit/undo/abandon: unreachable from MIA, Telegram and bearer (I4)


def test_no_mia_tool_reaches_owner_direct():
    for name in owner_tools.tool_names():
        spec = owner_tools.get_tool(name)
        assert not re.search(r"commit|undo|abandon", name), name
        assert not spec.handler.__module__.startswith("app.services.os_ledger"), name


def test_only_the_os_cookie_router_mounts_commit_paths():
    for method, path, kind in cells():
        if re.search(r"/commits(/|$)", path):
            assert kind == "owner_session" and path.startswith("/v1/owner/os/unified/"), path


def test_contexts_from_mia_telegram_and_bearer_can_never_commit_undo_or_abandon():
    settings = owner_settings()
    mia = authorize.from_mia_tool(Principal.owner(source="telegram", actor_id=OWNER_ID), settings)
    key = SimpleNamespace(
        workspace_id=settings.assaf_os_workspace_id, credential_id="AC_" + "c" * 32,
        owner_id=OWNER_ID, epoch=1, kind="claude",
        scopes="merchant_rules:read own_proposals:read statement:propose", revoked_at=None,
        expires_at=datetime.now(UTC) + timedelta(days=1))
    bearer = authorize.from_assistant(key, settings)
    workers = [authorize.for_worker(job, settings)
               for job in ("import", "mirror", "verifier", "authority", "retention")]
    # review r1: a TELEGRAM_APPROVAL context too (registered like a minted one).
    telegram = authorize._register(fake_ctx("TELEGRAM_APPROVAL", scopes=frozenset(),
                                            browser_mutation_verified=False))
    for ctx in (mia, telegram, bearer, *workers):
        for op in ("commit_direct", "undo", "abandon"):
            with pytest.raises(api.LedgerError) as exc:
                authorize.require(ctx, op)
            assert exc.value.code == "channel_operation_denied"
