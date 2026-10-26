"""E37 owner routes: request, Telegram approval, same-session one-time collection.

The real app uses its cookie/Origin/CSRF guards, SQLite session/card storage and FakeLedger.
No provider call is made; Telegram messages are recorded. The final flow uses and revokes the
collected key through the real bearer and owner routes.
"""

from __future__ import annotations

import pytest
from app.api.assistant import os as assistant_os
from app.api.os_v2 import owner_keys
from app.main import app
from app.services import assistant_keys as ac
from app.services.os_ledger import api

from tests.api.os_v2.v2_19_support import ORIGIN, OwnerApp, owner_settings
from tests.services.os_ledger.assistant.support import AWorld
from tests.services.os_ledger.statements.support import batch, encode, three_lines

ROOT = "/v1/owner/os/unified"
KEYS = ROOT + "/assistant-credentials"
OWNER = "123"
ENVELOPE = {"api_version", "request_id", "error"}
BINDING = {
    "api_version": "assaf-os.v2",
    "source": "postgresql",
    "workspace_id": "test",
    "authority_epoch": 1,
}



class World:
    """The app, an owner session, the FakeLedger world and helpers."""

    def __init__(self, owner: OwnerApp, aw: AWorld) -> None:
        self.owner, self.aw = owner, aw
        self.headers = owner.login()

    def fresh_headers(self) -> dict[str, str]:
        return self.owner.login()

    def call(self, method: str, path: str, *, headers=None, body=None, raw=None):
        h = dict(self.headers if headers is None else headers)
        kwargs = (
            {"content": raw, "headers": {**h, "Content-Type": "application/json"}}
            if (raw is not None)
            else {"json": body, "headers": h}
        )
        self.owner.client.cookies.clear()
        return self.owner.client.request(method, path, **kwargs)

    def request(self, *, headers=None, label="Claude desktop", kind="claude", days=None):
        body = {"label": label, "kind": kind}
        if days is not None:
            body["expires_in_days"] = days
        return self.call("POST", KEYS, headers=headers, body=body)

    def approve(self, request, *, decision="approve"):
        from app.capabilities.types import Principal
        from app.db.store import LeadStore
        from app.services.owner_actions import decide_owner_action, execute_assistant_issue_decision

        with self.owner.factory() as db:
            store = LeadStore(db)
            row = (
                store.list_all_pending_approvals()[0]
                if not hasattr(self, "card_id")
                else store.get_approval_by_approval_id(self.card_id)
            )
            self.card_id = row.approval_id
            decide_owner_action(
                store,
                principal=Principal.owner(source="telegram", actor_id=OWNER),
                approval_id=row.approval_id,
                decision="approved" if decision == "approve" else "rejected",
            )
            db.commit()
            return execute_assistant_issue_decision(
                store,
                settings=self.owner.settings,
                principal=Principal.owner(source="telegram", actor_id=OWNER),
                proposal_id=row.resource_id,
                decision=decision,
            )

    def collect(self, request, *, headers=None):
        return self.call(
            "POST",
            KEYS + "/issue-requests/" + request["issue_request_id"] + "/collect",
            headers=headers,
            body={},
        )

    def issue(self, *, headers=None, label="Claude desktop", kind="claude", days=None):
        response = self.request(headers=headers, label=label, kind=kind, days=days)
        if response.status_code != 202:
            return response
        self.card_id = None
        del self.card_id
        self.approve(response.json())
        return self.collect(response.json(), headers=headers)


@pytest.fixture
def world(monkeypatch):
    aw = AWorld("fake")
    monkeypatch.setattr(owner_keys, "service_for", lambda settings: aw.service)
    from app.services import owner_actions

    monkeypatch.setattr(owner_actions, "assistant_issue_service", lambda settings: aw.service)
    monkeypatch.setattr(assistant_os, "gateway_for", lambda settings: aw.gateway)
    settings = owner_settings(
        assaf_os_assistant_enabled=True,
        os_step_up_secret="s" * 32,
        owner_os_origin=ORIGIN,
        os_assistant_ip_hmac_key="i" * 32,
    )
    aw.w.config.owner_os_origin = ORIGIN
    from app.api.deps import get_db, get_telegram_port
    from app.db.models import ApprovalRow
    from app.integrations.base import RecordingMessagePort

    with OwnerApp(settings) as owner:
        ApprovalRow.__table__.create(owner.factory.kw["bind"])

        def db():
            with owner.factory() as session:
                yield session

        port = RecordingMessagePort()
        app.dependency_overrides[get_db] = db
        app.dependency_overrides[get_telegram_port] = lambda: port
        world = World(owner, aw)
        world.port = port
        try:
            yield world
        finally:
            app.dependency_overrides.pop(get_db, None)
            app.dependency_overrides.pop(get_telegram_port, None)


def error_of(response, status: int, code: str) -> dict:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == ENVELOPE and body["error"]["code"] == code, body
    return body["error"]


def mounted() -> set[tuple[str, str]]:
    return {(m.upper(), p) for p, operations in app.openapi()["paths"].items() for m in operations}


# ---------------------------------------------------------------------------------------------
# the route table


def test_exactly_the_key_routes_exist_and_step_up_is_retired():
    found = {(m, p) for m, p in mounted() if "assistant-credentials" in p or "step-up" in p}
    assert found == {
        ("GET", KEYS),
        ("POST", KEYS),
        ("POST", KEYS + "/{credential_id}/revoke"),
        ("POST", KEYS + "/revoke-all"),
        ("GET", KEYS + "/issue-requests/{issue_request_id}"),
        ("POST", KEYS + "/issue-requests/{issue_request_id}/collect"),
    }


# ---------------------------------------------------------------------------------------------
# cross-refusals: the owner routes are cookie-only


def test_a_bearer_key_is_refused_on_every_owner_key_route(world):
    issued = world.aw.issue()
    bearer = {"Authorization": f"Bearer {issued['secret']}"}
    revoke = KEYS + "/AC_" + "a" * 32 + "/revoke"
    for method, path in (
        ("GET", KEYS),
        ("POST", KEYS),
        ("POST", revoke),
        ("POST", KEYS + "/revoke-all"),
    ):
        for headers in (bearer, {**world.fresh_headers(), **bearer}):
            error_of(world.call(method, path, headers=headers, body={}), 401, "owner_auth_required")
    assert world.aw.credential(issued["credential_id"])["use_count"] == 0


def test_the_owner_routes_need_a_session_origin_and_csrf(world):
    cookie = world.headers["Cookie"]
    for method, path in (("GET", KEYS), ("POST", KEYS), ("POST", KEYS + "/revoke-all")):
        assert world.call(method, path, headers={"Origin": ORIGIN}, body={}).status_code == 401
        assert world.call(method, path, headers={"Cookie": cookie}, body={}).status_code == 403
    for path in (KEYS, KEYS + "/revoke-all"):
        no_csrf = {k: v for k, v in world.headers.items() if k != "X-CSRF-Token"}
        error_of(world.call("POST", path, headers=no_csrf, body={}), 403, "csrf_denied")
        bad = {**world.headers, "X-CSRF-Token": "f" * 64}
        error_of(world.call("POST", path, headers=bad, body={}), 403, "csrf_denied")


def test_a_session_without_the_approve_scope_cannot_list_or_manage(world):
    weak = world.owner.login(scopes=("assaf-os:read", "assaf-os:propose"))
    error_of(world.call("GET", KEYS, headers=weak), 403, "owner_scope_denied")
    error_of(world.call("POST", KEYS, headers=weak, body={}), 403, "owner_scope_denied")
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
        update={"assaf_os_assistant_enabled": False}
    )
    error_of(world.call("GET", KEYS), 404, "operation_unavailable")
    h = world.headers
    error_of(
        world.call("POST", KEYS, headers=h, body={"label": "x", "kind": "claude"}),
        404,
        "operation_unavailable",
    )


# ---------------------------------------------------------------------------------------------
# E37 collection


# ---------------------------------------------------------------------------------------------
# issue, revoke, revoke-all


def test_issue_returns_the_item_plus_the_secret_with_the_binding_and_no_version(world):
    response = world.issue()
    assert response.status_code == 200
    body = response.json()
    assert {k: body[k] for k in BINDING} == BINDING and "observed_version" not in body
    assert set(body) == BINDING.keys() | {
        "credential_id",
        "label",
        "kind",
        "created_at",
        "expires_at",
        "last_used_at",
        "use_count",
        "revoked_at",
        "secret",
    }
    assert body["secret"].startswith("aosp_") and len(body["secret"]) == 48
    listed = world.call("GET", KEYS).text
    assert body["secret"] not in listed and body["secret"] not in world.aw.everything()


def test_issue_validates_the_body_before_creating_a_request(world):
    h = world.headers
    for bad in (
        {},
        {"label": "x"},
        {"kind": "claude"},
        {"label": "x", "kind": "claude", "z": 1},
        {"label": "x", "kind": "robot"},
        {"label": "", "kind": "claude"},
        {"label": "x", "kind": "gpt", "expires_in_days": 91},
        {"label": "x", "kind": "gpt", "expires_in_days": "30"},
    ):
        error = error_of(world.call("POST", KEYS, headers=h, body=bad), 422, "validation_error")
        assert error["details"]["reason"] in {
            "invalid_body",
            "invalid_label",
            "invalid_kind",
            "invalid_expiry",
        }
    # the owner boundary already refuses a non-object, a duplicate key and invalid JSON (422)
    for raw in ("[]", '{"label":"a","label":"b","kind":"claude"}', "not json"):
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
    error_of(
        world.call("POST", f"{KEYS}/{issued['credential_id']}/revoke", body={"x": 1}),
        422,
        "validation_error",
    )


def test_revoking_an_unknown_key_is_a_validation_error(world):
    error = error_of(
        world.call("POST", f"{KEYS}/AC_{'e' * 32}/revoke", body={}), 422, "validation_error"
    )
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
# retired Google step-up tests were removed with the route


# ---------------------------------------------------------------------------------------------
# end to end


def test_telegram_approve_then_collect_then_use_and_revoke(world):
    aw = world.aw
    issued = world.issue(label="Claude desktop").json()
    bearer = {"Authorization": f"Bearer {issued['secret']}", "Content-Type": "application/json"}
    client = world.owner.client
    client.cookies.clear()
    context = client.get("/v1/assistant/os/context", headers=bearer)
    assert context.status_code == 200, context.text
    submitted = client.post(
        "/v1/assistant/os/statement-batches", content=encode(batch(three_lines())), headers=bearer
    )
    assert submitted.status_code == 200, submitted.text
    assert [p["status"] for p in aw.proposals()] == ["pending"]
    # the owner revokes: the key is dead and its pending batch is rejected in the same step
    assert (
        world.call("POST", f"{KEYS}/{issued['credential_id']}/revoke", body={}).status_code == 200
    )
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


def test_request_waits_for_telegram_and_status_is_bound_to_the_same_session(world):
    requested = world.request()
    assert requested.status_code == 202
    body = requested.json()
    assert body.keys() == BINDING.keys() | {"issue_request_id", "status", "expires_at"}
    assert body["status"] == "pending_approval" and world.aw.credential_rows() == []
    path = KEYS + "/issue-requests/" + body["issue_request_id"]
    assert world.call("GET", path).json() == body
    error_of(world.collect(body), 409, "issue_not_approved")
    other = world.fresh_headers()
    error_of(world.call("GET", path, headers=other), 404, "credential_unknown")
    error_of(world.collect(body, headers=other), 404, "credential_unknown")
    assert world.approve(body).status == "approved"
    no_csrf = {k: v for k, v in world.headers.items() if k != "X-CSRF-Token"}
    error_of(world.collect(body, headers=no_csrf), 403, "csrf_denied")
    no_origin = {k: v for k, v in world.headers.items() if k != "Origin"}
    assert world.collect(body, headers=no_origin).status_code == 403
    assert world.collect(body).status_code == 200
    error_of(world.collect(body), 409, "issue_collected")


def test_card_send_failure_keeps_committed_request_and_pending_card(world, monkeypatch, caplog):
    from app.db.store import LeadStore
    from app.domain.owner.proposal_cards import pending_approval_cards

    async def fail(message):
        with world.owner.factory() as db:
            assert len(LeadStore(db).list_all_pending_approvals()) == 1
        assert len(world.aw.issue_rows()) == 1
        raise RuntimeError("provider payload must not be logged")

    monkeypatch.setattr(world.port, "send", fail)
    response = world.request(label="<my Claude>")
    assert response.status_code == 202
    with world.owner.factory() as db:
        cards, _ = pending_approval_cards(LeadStore(db))
    assert len(cards) == 1 and "&lt;my Claude&gt;" in cards[0][1]
    assert "card_send_failed" in caplog.text and "provider payload" not in caplog.text
    assert world.aw.issue_rows()[0]["status"] == "pending_approval"


def test_failed_ledger_cas_then_retap_retries_without_sticky_unknown(world, monkeypatch):
    request = world.request().json()
    original = world.aw.service.decide_issue

    def fail(*args, **kwargs):
        raise api.LedgerError("ledger_unavailable")

    monkeypatch.setattr(world.aw.service, "decide_issue", fail)
    assert world.approve(request).status == "not_started"
    assert world.aw.issue_rows()[0]["status"] == "pending_approval"
    monkeypatch.setattr(world.aw.service, "decide_issue", original)
    assert world.approve(request).status == "approved"
    assert world.collect(request).status_code == 200


@pytest.mark.asyncio
async def test_telegram_issue_callback_runs_off_loop_on_a_thread_owned_session(world, monkeypatch):
    import asyncio
    import threading
    import time

    from app.api import telegram
    from app.db import session as db_session
    from app.db.store import LeadStore
    from app.services import owner_actions

    request = world.request().json()
    with world.owner.factory() as db:
        row = LeadStore(db).list_all_pending_approvals()[0]
        token = row.approval_id
    main_thread = threading.get_ident()
    original = owner_actions.execute_assistant_issue_decision

    def blocking(store, **kwargs):
        assert threading.get_ident() != main_thread
        assert store.session is not callback_db
        with world.owner.factory() as probe:
            assert LeadStore(probe).get_approval_by_approval_id(token).decision == "approved"
        time.sleep(0.3)
        return original(store, **kwargs)

    monkeypatch.setattr(owner_actions, "execute_assistant_issue_decision", blocking)
    monkeypatch.setattr(db_session, "get_session_factory", lambda: world.owner.factory)
    ticks = 0
    stop = asyncio.Event()

    async def ticker():
        nonlocal ticks
        while not stop.is_set():
            await asyncio.sleep(0.01)
            ticks += 1

    task = asyncio.create_task(ticker())
    try:
        with world.owner.factory() as callback_db:
            result = await telegram._handle_callback(
                callback={
                    "callback_query_id": "q-test",
                    "from": OWNER,
                    "data": "ok:" + token,
                    "chat_id": OWNER,
                    "chat_type": "private",
                    "message_id": "71",
                },
                port=world.port,
                owner_ids={OWNER},
                db=callback_db,
                settings=world.owner.settings,
            )
    finally:
        stop.set()
        await task
    assert result["decision"] == "approve"
    assert ticks >= 10
    assert world.collect(request).status_code == 200
