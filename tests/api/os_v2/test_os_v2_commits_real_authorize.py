"""V2-24 D3: the owner-direct routes run on V2-19's REAL ``authorize`` (V2_PG_CONTRACTS.md §9).

``test_os_v2_commits.py`` stands a fake ``authorize`` in, which was the only way to run the routes
before V2-19 landed. These cases use the real app (``app.main.app``), the real owner-session
machinery and the real ``authorize`` module, over the V2-16f FakeLedger, so they prove the pieces
V2-24 relies on exist and fit: ``assaf-os:direct`` in the scope allow-lists, ``from_owner_mutation``
and ``from_owner_read`` minting the context, and ``require(ctx, op)`` for ``commit_direct``,
``undo``, ``abandon`` and ``read``.
"""

from __future__ import annotations

import uuid

import pytest
from app.api import owner_sessions
from app.services.os_ledger import api, authorize, owner_direct

from tests.api.os_v2.v2_19_support import OS_SCOPES, OwnerApp, ledger_world, owner_settings
from tests.fakes.os_ledger_builders import CLIENT_ACTIVE, PATH_LEARN, WORK_ACTIVE

ROOT = "/v1/owner/os/unified"
DIRECT = "assaf-os:direct"
DIRECT_OPS = ("commit_direct", "undo", "abandon")
TASK_OPEN = "לביצוע"


def uuid4_str() -> str:
    return str(uuid.uuid4())


@pytest.fixture
def world(monkeypatch):
    env = ledger_world()
    monkeypatch.setattr(api, "build_ledger", lambda _settings: env.fake)
    return env


@pytest.fixture
def owner(world):
    with OwnerApp() as bound:
        yield bound


@pytest.fixture
def calls(monkeypatch):
    """Record every call into the real constructors and ``require``, still running them."""
    seen: list[tuple[str, str]] = []
    for name in ("from_owner_mutation", "from_owner_read"):
        original = getattr(authorize, name)

        def wrapper(*args, _name=name, _original=original, **kwargs):
            seen.append((_name, ""))
            return _original(*args, **kwargs)

        monkeypatch.setattr(authorize, name, wrapper)
    original_require = authorize.require

    def require(ctx, op):
        seen.append(("require", op))
        return original_require(ctx, op)

    monkeypatch.setattr(authorize, "require", require)
    return seen


def create_body(request_id: str) -> dict:
    return {"request_id": request_id, "op": "create", "entity": "tasks", "record_id": None,
            "expected_revision": 0, "patch": {"title": "Task", "status": TASK_OPEN}}


def error(response) -> str:
    return response.json()["error"]["code"]


# --- what V2-19 has to provide ------------------------------------------------------------------


def test_the_direct_scope_is_registered_where_v2_24_needs_it():
    assert DIRECT == authorize.DIRECT_SCOPE == owner_direct.DIRECT_SCOPE
    assert DIRECT in owner_sessions.KNOWN_SCOPES
    assert DIRECT in owner_sessions.ASSAF_OS_WRITE_SCOPES
    assert DIRECT in OS_SCOPES
    owner_sessions.owner_mutation_scope(DIRECT)  # raises ValueError for a scope off the allow-list


@pytest.mark.parametrize("op", DIRECT_OPS)
def test_the_matrix_lets_only_a_verified_os_session_with_the_direct_scope_do(op):
    row = authorize.MATRIX[op]
    assert row["OS"] == ("scope", DIRECT, True), "the direct scope, with a verified mutation"
    assert {column: cell[0] for column, cell in row.items() if column != "OS"} == {
        "MIA": "deny", "TELEGRAM_APPROVAL": "deny", "ASSISTANT": "deny", "WORKER": "deny"}
    assert all(cell[1] == "channel_operation_denied" for column, cell in row.items()
               if column != "OS")


def test_the_matrix_reads_with_the_read_scope_and_no_mutation_proof():
    assert authorize.MATRIX["read"]["OS"] == ("scope", "assaf-os:read", False)


# --- the routes, end to end ---------------------------------------------------------------------


def test_the_four_routes_mint_and_check_through_the_real_authorize(owner, world, calls):
    headers = owner.login()
    created = owner.client.post(ROOT + "/commits", json=create_body(uuid4_str()), headers=headers)
    assert created.status_code == 200, created.text
    body = created.json()
    assert (body["status"], body["kind"], body["actor_source"]) == (
        "committed", "owner_direct", "OS_DIRECT")
    assert body["workspace_id"] == owner.settings.assaf_os_workspace_id
    undone = owner.client.post(ROOT + f"/commits/{body['request_id']}/undo",
                               json={"undo_request_id": uuid4_str()}, headers=headers)
    assert undone.status_code == 200, undone.text
    assert (undone.json()["kind"], undone.json()["undo_of"]) == ("owner_undo", body["request_id"])
    abandoned = owner.client.post(ROOT + f"/commits/{uuid4_str()}/abandon", json={},
                                  headers=headers)
    assert abandoned.status_code == 200 and abandoned.json()["status"] == "abandoned"
    listed = owner.client.get(ROOT + "/deleted", headers=headers)
    assert listed.status_code == 200, listed.text
    assert [r["task_id"] for r in listed.json()["records"]] == [
        body["records"][0]["record"]["task_id"]]
    assert calls == [
        ("from_owner_mutation", ""), ("require", "commit_direct"),
        ("from_owner_mutation", ""), ("require", "undo"),
        ("from_owner_mutation", ""), ("require", "abandon"),
        ("from_owner_read", ""), ("require", "read"),
    ]


def test_an_undo_with_a_live_dependent_is_a_409_undo_conflict_and_writes_nothing(owner, world):
    """§22 E26 over HTTP: ``undo_conflict``, never the ledger's own ``has_dependents``."""
    headers = owner.login()
    client = owner.client.post(ROOT + "/commits", json={
        "request_id": uuid4_str(), "op": "create", "entity": "clients", "record_id": None,
        "expected_revision": 0, "patch": {"name": "C", "status": CLIENT_ACTIVE}}, headers=headers)
    person = client.json()["records"][0]["record"]["person_id"]
    created_work = owner.client.post(ROOT + "/commits", json={
        "request_id": uuid4_str(), "op": "create", "entity": "works", "record_id": None,
        "expected_revision": 0, "patch": {"person_id": person, "title": "W", "path": PATH_LEARN,
                                          "status": WORK_ACTIVE, "currency": "ILS"}},
        headers=headers)
    assert created_work.status_code == 200, created_work.text
    work_id = created_work.json()["records"][0]["record"]["work_id"]
    version = world.version()
    refused = owner.client.post(ROOT + f"/commits/{client.json()['request_id']}/undo",
                                json={"undo_request_id": uuid4_str()}, headers=headers)
    assert refused.status_code == 409, refused.text
    envelope = refused.json()["error"]
    assert envelope["code"] == "undo_conflict" and envelope["write_outcome"] == "not_started"
    assert envelope["details"] == {"reason": "dependent_exists", "changed": [
        {"entity": "works", "record_id": work_id}]}
    assert world.version() == version


def test_a_session_without_the_direct_scope_is_refused_before_authorize(owner, world, calls):
    headers = owner.login(scopes=("assaf-os:read", "assaf-os:propose", "assaf-os:approve"))
    response = owner.client.post(ROOT + "/commits", json=create_body(uuid4_str()), headers=headers)
    assert (response.status_code, error(response)) == (403, "owner_scope_denied")
    assert calls == [] and world.version() == 0


def test_a_session_without_the_read_scope_cannot_list_deleted(owner, world, calls):
    headers = owner.login(scopes=(DIRECT,))
    response = owner.client.get(ROOT + "/deleted", headers=headers)
    assert (response.status_code, error(response)) == (403, "owner_scope_denied")
    assert calls == []


@pytest.mark.parametrize("method,path", [
    ("POST", "/commits"), (  # the path ids are canonical uuid4 strings
        "POST", "/commits/00000000-0000-4000-8000-000000000001/undo"),
    ("POST", "/commits/00000000-0000-4000-8000-000000000001/abandon"), ("GET", "/deleted")])
def test_an_authorization_header_is_refused_before_authorize(owner, world, calls, method, path):
    headers = owner.login() | {"Authorization": "Bearer aosp_" + "a" * 43}
    response = owner.client.request(method, ROOT + path, headers=headers,
                                    json=create_body(uuid4_str()) if method == "POST" else None)
    assert (response.status_code, error(response)) == (401, "owner_auth_required")
    assert calls == [] and world.version() == 0


def test_a_session_owner_outside_the_allow_list_is_refused_before_any_context(world, calls):
    """The session layer refuses an owner outside the allow-list (401) before any constructor runs,
    so nothing is minted. The constructor's own allow-list re-check is pinned by V2-19's
    tests/services/os_ledger/test_authorize_constructors.py."""
    with OwnerApp(owner_settings(telegram_owner_user_ids="999")) as other:
        response = other.client.post(ROOT + "/commits", json=create_body(uuid4_str()),
                                     headers=other.login())
    assert response.status_code == 401 and error(response) == "owner_auth_required"
    assert calls == [] and world.version() == 0
