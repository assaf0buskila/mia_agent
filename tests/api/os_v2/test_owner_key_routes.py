"""V2-37, the owner's key routes and the step-up route (V2_PG_CONTRACTS.md §17.1, §9, §10.5).

The real ``app.main.app`` runs with the real owner-session machinery (cookie, exact Origin, CSRF)
over the sqlite session store of ``v2_19_support.OwnerApp``, V2-19's real ``authorize``, and the
FakeLedger world of ``tests/services/os_ledger/assistant``. The Google proof is a real RS256 token
signed with a throw-away key (the JWKS cache is replaced), so the step-up route runs the whole
verification. A last test goes end to end: step-up, issue, use the key as a bearer, revoke, refused.
"""

from __future__ import annotations

import json
import time

import jwt
import pytest
from app.api.assistant import os as assistant_os
from app.api.os_v2 import owner_keys
from app.main import app
from app.services import assistant_keys as ac
from app.services import assistant_step_up as step_up
from app.services import owner_google_login as google
from app.services.os_ledger import api

from tests.api.os_v2.v2_19_support import ORIGIN, OwnerApp, owner_settings
from tests.services.os_ledger.assistant.support import AWorld
from tests.services.os_ledger.statements.support import batch, encode, three_lines
from tests.services.test_assistant_step_up import JWK, KEY, SUBJECT

ROOT = "/v1/owner/os/unified"
KEYS = ROOT + "/assistant-credentials"
STEP_UP = "/v1/owner/auth/step-up"
CLIENT = "client.example"
OWNER = "123"
ENVELOPE = {"api_version", "request_id", "error"}
BINDING = {"api_version": "assaf-os.v2", "source": "postgresql", "workspace_id": "test",
           "authority_epoch": 1}
GOOGLE = dict(owner_os_google_enabled=True, owner_os_google_client_id=CLIENT,
              owner_os_google_subject=SUBJECT, owner_os_google_owner_id=OWNER)


class Jwks:
    def get(self, kid):
        if kid != "k1":
            raise google.GoogleOwnerLoginDenied("owner authentication required")
        return JWK


def google_token(nonce: str, *, auth_time_age: int = 5, **over) -> str:
    now = int(time.time())
    claims = {"iss": "https://accounts.google.com", "sub": SUBJECT, "aud": CLIENT,
              "iat": now - 5, "exp": now + 300, "nonce": nonce, "auth_time": now - auth_time_age}
    claims.update(over)
    return jwt.encode(claims, KEY, algorithm="RS256", headers={"kid": "k1"})


class World:
    """The app, an owner session, the FakeLedger world and helpers."""

    def __init__(self, owner: OwnerApp, aw: AWorld) -> None:
        self.owner, self.aw = owner, aw
        self.headers = owner.login()

    def fresh_headers(self) -> dict[str, str]:
        return self.owner.login()

    def call(self, method: str, path: str, *, headers=None, body=None, raw=None):
        h = dict(self.headers if headers is None else headers)
        kwargs = {"content": raw, "headers": {**h, "Content-Type": "application/json"}} if (
            raw is not None) else {"json": body, "headers": h}
        self.owner.client.cookies.clear()
        return self.owner.client.request(method, path, **kwargs)

    def step_up_token(self, headers=None) -> str:
        h = self.headers if headers is None else headers
        response = self.call("POST", STEP_UP, headers=h,
                             body={"id_token": google_token(h["X-CSRF-Token"])})
        assert response.status_code == 200, response.text
        return response.json()["step_up_token"]

    def issue(self, *, token=None, headers=None, label="Claude desktop", kind="claude", days=None):
        h = dict(self.headers if headers is None else headers)
        h["X-Step-Up"] = self.step_up_token(headers) if token is None else token
        body = {"label": label, "kind": kind}
        if days is not None:
            body["expires_in_days"] = days
        return self.call("POST", KEYS, headers=h, body=body)


@pytest.fixture
def world(monkeypatch):
    aw = AWorld("fake")
    monkeypatch.setattr(google, "_JWKS", Jwks())
    monkeypatch.setattr(owner_keys, "service_for", lambda settings: ac.AssistantCredentialService(
        aw.w.ledger, settings, aw.statements))
    monkeypatch.setattr(assistant_os, "gateway_for", lambda settings: aw.gateway)
    settings = owner_settings(
        assaf_os_assistant_enabled=True, os_step_up_secret="s" * 32, owner_os_origin=ORIGIN,
        os_assistant_ip_hmac_key="i" * 32, **GOOGLE)
    aw.w.config.owner_os_origin = ORIGIN
    with OwnerApp(settings) as owner:
        yield World(owner, aw)


def error_of(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == ENVELOPE and body["error"]["code"] == code, body
    return body["error"]


def mounted() -> set[tuple[str, str]]:
    return {(m.upper(), p) for p, operations in app.openapi()["paths"].items() for m in operations}


# ---------------------------------------------------------------------------------------------
# the route table


def test_exactly_the_key_routes_and_the_step_up_route_exist():
    found = {(m, p) for m, p in mounted() if "assistant-credentials" in p or "step-up" in p}
    assert found == {
        ("GET", KEYS), ("POST", KEYS), ("POST", KEYS + "/{credential_id}/revoke"),
        ("POST", KEYS + "/revoke-all"), ("POST", STEP_UP)}


# ---------------------------------------------------------------------------------------------
# cross-refusals: the owner routes are cookie-only


def test_a_bearer_key_is_refused_on_every_owner_key_route(world):
    issued = world.aw.issue()
    bearer = {"Authorization": f"Bearer {issued['secret']}"}
    revoke = KEYS + "/AC_" + "a" * 32 + "/revoke"
    for method, path in (("GET", KEYS), ("POST", KEYS), ("POST", revoke),
                         ("POST", KEYS + "/revoke-all"), ("POST", STEP_UP)):
        for headers in (bearer, {**world.fresh_headers(), **bearer}):
            error_of(world.call(method, path, headers=headers, body={}), 401,
                     "owner_auth_required")
    assert world.aw.credential(issued["credential_id"])["use_count"] == 0


def test_the_owner_routes_need_a_session_origin_and_csrf(world):
    cookie = world.headers["Cookie"]
    for method, path in (("GET", KEYS), ("POST", KEYS), ("POST", KEYS + "/revoke-all"),
                         ("POST", STEP_UP)):
        assert world.call(method, path, headers={"Origin": ORIGIN}, body={}).status_code == 401
        assert world.call(method, path, headers={"Cookie": cookie}, body={}).status_code == 403
    for path in (KEYS, KEYS + "/revoke-all", STEP_UP):
        no_csrf = {k: v for k, v in world.headers.items() if k != "X-CSRF-Token"}
        error_of(world.call("POST", path, headers=no_csrf, body={}), 403, "csrf_denied")
        bad = {**world.headers, "X-CSRF-Token": "f" * 64}
        error_of(world.call("POST", path, headers=bad, body={}), 403, "csrf_denied")


def test_a_session_without_the_approve_scope_cannot_list_or_manage(world):
    weak = world.owner.login(scopes=("assaf-os:read", "assaf-os:propose"))
    error_of(world.call("GET", KEYS, headers=weak), 403, "owner_scope_denied")
    error_of(world.call("POST", KEYS, headers=weak, body={}), 403, "owner_scope_denied")
    error_of(world.call("POST", STEP_UP, headers=weak, body={}), 403, "owner_scope_denied")
    assert world.aw.credential_rows() == []


# ---------------------------------------------------------------------------------------------
# the list


def test_the_list_is_bound_and_carries_no_version_and_no_secret(world):
    issued = world.aw.issue()
    response = world.call("GET", KEYS)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == BINDING.keys() | {"credentials"} and "observed_version" not in body
    assert {k: body[k] for k in BINDING} == BINDING
    [item] = body["credentials"]
    assert item["credential_id"] == issued["credential_id"] and "secret" not in item
    assert issued["secret"] not in response.text
    assert response.headers["cache-control"] == "private, no-store"


def test_a_disabled_assistant_feature_is_404_after_authentication(world):
    world.owner.settings = world.owner.settings.model_copy(
        update={"assaf_os_assistant_enabled": False})
    error_of(world.call("GET", KEYS), 404, "operation_unavailable")
    h = {**world.headers, "X-Step-Up": "su1.1.2." + "0" * 64}
    error_of(world.call("POST", KEYS, headers=h, body={"label": "x", "kind": "claude"}), 404,
             "operation_unavailable")
    error_of(world.call("POST", STEP_UP, body={"id_token": "x"}), 404, "operation_unavailable")


# ---------------------------------------------------------------------------------------------
# step-up is required to issue


def test_issuing_without_a_step_up_token_is_403_step_up_required(world):
    body = {"label": "Claude", "kind": "claude"}
    for headers in (world.headers, {**world.headers, "X-Step-Up": ""},
                    {**world.headers, "X-Step-Up": "nonsense"}):
        error = error_of(world.call("POST", KEYS, headers=headers, body=body), 403,
                         "step_up_required")
        assert error == {"code": "step_up_required", "retryable": False,
                         "write_outcome": "not_started"}
    assert world.aw.credential_rows() == []


def test_a_step_up_token_of_another_session_is_refused(world):
    other = world.fresh_headers()
    token = world.step_up_token(other)  # minted for the OTHER session
    error_of(world.issue(token=token), 403, "step_up_required")
    assert world.aw.credential_rows() == []
    assert world.issue(token=world.step_up_token()).status_code == 200


def test_an_expired_step_up_token_is_refused(world, monkeypatch):
    token = world.step_up_token()
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 601)
    error_of(world.issue(token=token), 403, "step_up_required")
    assert world.aw.credential_rows() == []


def test_a_forged_step_up_token_is_refused(world):
    minted = world.step_up_token()
    version, auth_time, exp, mac = minted.split(".")
    forged = ".".join([version, auth_time, str(int(exp) + 600), mac])
    error_of(world.issue(token=forged), 403, "step_up_required")
    secretless = step_up.mint(session_hash="0" * 64, auth_time=int(auth_time),
                              now=int(time.time()), settings=world.owner.settings).token
    error_of(world.issue(token=secretless), 403, "step_up_required")


# ---------------------------------------------------------------------------------------------
# issue, revoke, revoke-all


def test_issue_returns_the_item_plus_the_secret_with_the_binding_and_no_version(world):
    response = world.issue()
    assert response.status_code == 200
    body = response.json()
    assert {k: body[k] for k in BINDING} == BINDING and "observed_version" not in body
    assert set(body) == BINDING.keys() | {
        "credential_id", "label", "kind", "created_at", "expires_at", "last_used_at",
        "use_count", "revoked_at", "secret"}
    assert body["secret"].startswith("aosp_") and len(body["secret"]) == 48
    listed = world.call("GET", KEYS).text
    assert body["secret"] not in listed and body["secret"] not in world.aw.everything()


def test_issue_validates_the_body_after_the_step_up(world):
    token = world.step_up_token()
    h = {**world.headers, "X-Step-Up": token}
    for bad in ({}, {"label": "x"}, {"kind": "claude"}, {"label": "x", "kind": "claude", "z": 1},
                {"label": "x", "kind": "robot"}, {"label": "", "kind": "claude"},
                {"label": "x", "kind": "gpt", "expires_in_days": 91},
                {"label": "x", "kind": "gpt", "expires_in_days": "30"}):
        error = error_of(world.call("POST", KEYS, headers=h, body=bad), 422, "validation_error")
        assert error["details"]["reason"] in {
            "invalid_body", "invalid_label", "invalid_kind", "invalid_expiry"}
    # the owner boundary already refuses a non-object, a duplicate key and invalid JSON (422)
    for raw in ('[]', '{"label":"a","label":"b","kind":"claude"}', "not json"):
        error_of(world.call("POST", KEYS, headers=h, raw=raw), 422, "validation_error")
    assert world.aw.credential_rows() == []


def test_revoke_rejects_the_keys_pending_batches_and_returns_the_list(world):
    aw = world.aw
    issued = aw.issue()
    submitted = aw.paste_as(issued, batch(three_lines()))
    response = world.call("POST", f"{KEYS}/{issued['credential_id']}/revoke", body={})
    assert response.status_code == 200
    body = response.json()
    assert {k: body[k] for k in BINDING} == BINDING and "observed_version" not in body
    assert body["credentials"][0]["revoked_at"] is not None
    assert aw.w.review(submitted.proposal_id)["status"] == "rejected"
    # an empty body is as good as {}; anything else is refused
    assert world.call("POST", f"{KEYS}/{issued['credential_id']}/revoke", raw="").status_code == 200
    error_of(world.call("POST", f"{KEYS}/{issued['credential_id']}/revoke", body={"x": 1}), 422,
             "validation_error")


def test_revoking_an_unknown_key_is_a_validation_error(world):
    error = error_of(world.call("POST", f"{KEYS}/AC_{'e' * 32}/revoke", body={}), 422,
                     "validation_error")
    assert error["details"] == {"reason": "credential_unknown"}
    error_of(world.call("POST", f"{KEYS}/not-a-key/revoke", body={}), 422, "validation_error")


def test_revoke_all_revokes_every_key_and_returns_the_list(world):
    aw = world.aw
    one, two = aw.issue(label="one"), aw.issue(label="two", kind="gpt")
    aw.paste_as(one, batch(three_lines(), number=1, label="Card One"))
    response = world.call("POST", KEYS + "/revoke-all", body={})
    assert response.status_code == 200
    assert all(i["revoked_at"] is not None for i in response.json()["credentials"])
    assert {p["status"] for p in aw.proposals()} == {"rejected"}
    assert two["credential_id"] in response.text


# ---------------------------------------------------------------------------------------------
# the step-up route


def test_the_step_up_returns_exactly_the_token_and_its_expiry(world):
    response = world.call("POST", STEP_UP, body={"id_token": google_token(
        world.headers["X-CSRF-Token"])})
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"step_up_token", "expires_at"}
    assert body["step_up_token"].startswith("su1.")
    assert body["expires_at"].endswith("Z") and "T" in body["expires_at"]
    assert response.headers["cache-control"] == "private, no-store"
    assert "write_outcome" not in json.dumps(body)


def test_a_stale_auth_time_a_wrong_nonce_or_a_bad_token_is_403_step_up_required(world):
    csrf = world.headers["X-CSRF-Token"]
    for supplied in (google_token(csrf, auth_time_age=601), google_token("another-nonce"),
                     google_token(csrf, sub="999"), google_token(csrf, aud="other.example"),
                     "not-a-jwt", google_token(csrf)[:-4] + "AAAA"):
        error = error_of(world.call("POST", STEP_UP, body={"id_token": supplied}), 403,
                         "step_up_required")
        assert error == {"code": "step_up_required", "retryable": False}  # no write_outcome


def test_the_step_up_body_is_exactly_an_id_token(world):
    for bad in ({}, {"id_token": ""}, {"id_token": 5}, {"id_token": "x", "extra": 1}, []):
        error_of(world.call("POST", STEP_UP, body=bad), 422, "validation_error")
    error_of(world.call("POST", STEP_UP, raw="nope"), 422, "validation_error")


def test_a_missing_step_up_secret_or_google_binding_is_503_not_a_403(world):
    token = google_token(world.headers["X-CSRF-Token"])
    world.owner.settings = world.owner.settings.model_copy(update={"os_step_up_secret": ""})
    error_of(world.call("POST", STEP_UP, body={"id_token": token}), 503, "ledger_unavailable")
    world.owner.settings = world.owner.settings.model_copy(
        update={"os_step_up_secret": "s" * 32, "owner_os_google_client_id": ""})
    error_of(world.call("POST", STEP_UP, body={"id_token": token}), 503, "ledger_unavailable")


def test_an_unavailable_google_key_source_is_503(world, monkeypatch):
    def down(*_a, **_k):
        raise google.GoogleJwksUnavailable("google keys unavailable")

    monkeypatch.setattr(google, "verify_step_up_id_token", down)
    token = google_token(world.headers["X-CSRF-Token"])
    error_of(world.call("POST", STEP_UP, body={"id_token": token}), 503, "ledger_unavailable")


def test_the_step_up_token_is_not_logged_or_echoed_in_errors(world, caplog):
    caplog.set_level("DEBUG")
    token = world.step_up_token()
    world.issue(token=token + "0")
    assert token not in caplog.text and "su1." not in caplog.text


# ---------------------------------------------------------------------------------------------
# end to end


def test_step_up_then_issue_then_the_key_works_as_a_bearer_until_it_is_revoked(world):
    aw = world.aw
    issued = world.issue(label="Claude desktop").json()
    bearer = {"Authorization": f"Bearer {issued['secret']}",
              "Content-Type": "application/json"}
    client = world.owner.client
    client.cookies.clear()
    context = client.get("/v1/assistant/os/context", headers=bearer)
    assert context.status_code == 200, context.text
    submitted = client.post("/v1/assistant/os/statement-batches",
                            content=encode(batch(three_lines())), headers=bearer)
    assert submitted.status_code == 200, submitted.text
    assert [p["status"] for p in aw.proposals()] == ["pending"]
    # the owner revokes: the key is dead and its pending batch is rejected in the same step
    assert world.call("POST", f"{KEYS}/{issued['credential_id']}/revoke", body={}
                      ).status_code == 200
    client.cookies.clear()
    refused = client.get("/v1/assistant/os/context", headers=bearer)
    assert refused.status_code == 401
    assert [p["status"] for p in aw.proposals()] == ["rejected"]
    row = aw.credential(issued["credential_id"])
    assert row["use_count"] == 2 and row["revoked_at"] is not None


def test_the_service_is_built_from_the_ledger_when_not_replaced(monkeypatch):
    seen = {}
    monkeypatch.setattr(api, "build_ledger", lambda settings: seen.setdefault("ledger", object()))
    service = owner_keys.service_for(owner_settings())
    assert isinstance(service, ac.AssistantCredentialService) and "ledger" in seen
