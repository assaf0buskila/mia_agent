"""V2-19 (V2_PG_CONTRACTS.md §9, §10.1, §10.2, §22 E12/E19): ``GET /pending`` and
``GET /proposal-requests/{uuid}`` under ``/v1/owner/os/unified``.

Both routes are mounted from ``app/main.py`` (E19), so the backend is the gate: under ``sheets``
they answer 404 ``operation_unavailable`` and ``/proposal-requests`` stays absent exactly as on
v1. Any ``Authorization`` header is 401 (I4). The bodies follow §10.1 + §10.2 + E12 exactly; the
ledger is the V2-16f FakeLedger standing in for ``api.build_ledger``.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from app.services.assaf_os import INPUT_FIELDS
from app.services.os_ledger import api

from tests.api.os_v2.v2_19_support import OwnerApp, ledger_world, owner_settings
from tests.fakes.os_ledger_builders import commit_direct, ctx, propose_task, task
from tests.fakes.os_ledger_env import WORKSPACE

ROOT = "/v1/owner/os/unified"
UNKNOWN = "12345678-1234-4234-8234-123456789012"
BINDING = {"api_version": "assaf-os.v2", "source": "postgresql", "workspace_id": WORKSPACE,
           "authority_epoch": 1}
PROPOSAL_KEYS = {
    "api_version", "source", "workspace_id", "authority_epoch", "status", "proposal_id",
    "request_id", "approval_request_id", "rejection_request_id", "approval_guard_version",
    "actor_source", "kind", "expires_at", "entity", "record_id", "expected_revision", "input",
    "before", "after",
}
RECEIPT_KEYS = {
    "api_version", "source", "workspace_id", "authority_epoch", "observed_version", "status",
    "request_id", "proposal_id", "kind", "actor_source", "commit_version", "committed_at",
    "server_now", "records", "undo", "undo_of", "reason_codes",
}
REQUEST_KEYS = {
    "api_version", "source", "workspace_id", "authority_epoch", "observed_version", "request_id",
    "status", "proposal", "receipt", "reason_codes",
}


@pytest.fixture
def world(monkeypatch):
    env = ledger_world()
    monkeypatch.setattr(api, "build_ledger", lambda _settings: env.fake)
    return env


@pytest.fixture
def owner(world):
    with OwnerApp() as bound:
        yield bound


def get(owner, path, headers=None, **kwargs):
    return owner.client.get(ROOT + path, headers=owner.login() if headers is None else headers,
                            **kwargs)


def error(response) -> str:
    return response.json()["error"]["code"]


# ---------------------------------------------------------------------------------------------
# Gates: backend first (404), then Authorization (401), then the cookie session + read scope.


@pytest.mark.parametrize("backend", ["sheets", "maybe"])  # an invalid value coerces to sheets
@pytest.mark.parametrize("path", ["/pending", f"/proposal-requests/{UNKNOWN}"])
def test_the_routes_are_absent_unless_the_backend_is_postgres(world, backend, path, monkeypatch):
    monkeypatch.setattr(api, "build_ledger", lambda _s: pytest.fail("ledger built"))
    with OwnerApp(owner_settings(assaf_os_backend=backend)) as owner:
        assert owner.settings.assaf_os_backend == "sheets"
        for headers in (owner.login(), {}, {"Authorization": "Bearer x"}):
            response = owner.client.get(ROOT + path, headers=headers)
            assert response.status_code == 404, response.text
            assert error(response) == "operation_unavailable"


@pytest.mark.parametrize("path", ["/pending", f"/proposal-requests/{UNKNOWN}"])
@pytest.mark.parametrize("value", ["Bearer aosp_" + "a" * 43, "Basic eDp5", ""])
def test_any_authorization_header_is_401_even_with_a_valid_session(owner, path, value,
                                                                   monkeypatch):
    monkeypatch.setattr(api, "build_ledger", lambda _s: pytest.fail("ledger built"))
    for name in ("Authorization", "authorization"):
        response = get(owner, path, headers=owner.login() | {name: value})
        assert response.status_code == 401, response.text
        assert error(response) == "owner_auth_required"


@pytest.mark.parametrize("path", ["/pending", f"/proposal-requests/{UNKNOWN}"])
def test_a_session_with_the_read_scope_and_the_exact_origin_is_required(owner, path):
    assert owner.client.get(ROOT + path, headers={"Origin": "https://owner.assafweb.test"}
                            ).status_code == 401
    no_origin = owner.login()
    del no_origin["Origin"]
    assert get(owner, path, headers=no_origin).status_code == 403
    other = owner.login() | {"Origin": "https://evil.example"}
    assert get(owner, path, headers=other).status_code == 403
    propose_only = owner.login(scopes=("assaf-os:propose", "assaf-os:approve"))
    assert get(owner, path, headers=propose_only).status_code == 403


def test_reads_fail_closed_until_the_ledger_core_exists(monkeypatch):
    monkeypatch.setattr(api, "_implementation", lambda *_a: (_ for _ in ()).throw(
        api.LedgerError("ledger_unavailable", reason="implementation_missing")))
    with OwnerApp() as owner:
        for path in ("/pending", f"/proposal-requests/{UNKNOWN}"):
            response = get(owner, path)
            assert response.status_code == 503
            assert error(response) == "ledger_unavailable"
            assert "write_outcome" not in response.json()["error"]  # a read route


# ---------------------------------------------------------------------------------------------
# GET /pending


def test_pending_lists_the_pending_proposals_in_the_v2_shape(owner, world):
    first = propose_task(world, title="ראשונה")
    second = propose_task(world, ctx("MIA", browser_mutation_verified=False), title="שנייה")
    response = get(owner, "/pending")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    body = response.json()
    assert set(body) == set(BINDING) | {"observed_version", "items", "next_cursor"}
    assert {k: body[k] for k in BINDING} == BINDING
    assert body["observed_version"] == world.version() == 0
    assert body["next_cursor"] is None
    assert [item["proposal_id"] for item in body["items"]] == [first.proposal_id,
                                                                second.proposal_id]
    for item, row in zip(body["items"], (first, second), strict=True):
        assert set(item) == PROPOSAL_KEYS
        assert {k: item[k] for k in BINDING} == BINDING
        assert item["status"] == "pending" and item["kind"] == "proposal"
        assert item["approval_guard_version"] == 1
        assert (item["request_id"], item["approval_request_id"], item["rejection_request_id"]) == (
            row.request_id, row.approval_id, row.rejection_id)
        assert item["actor_source"] == row.source
        assert item["expires_at"].endswith("Z") and len(item["expires_at"]) == 24
        assert (item["entity"], item["record_id"], item["expected_revision"]) == (
            "tasks", row.record_id, 0)
        assert item["before"] is None
        assert item["after"] == dict(row.mutations[0].after)
        assert set(item["input"]) <= INPUT_FIELDS["tasks"]
    assert [i["actor_source"] for i in body["items"]] == ["OS", "MIA"]
    assert body["items"][0]["input"]["title"] == "ראשונה"


def test_pending_pages_with_the_opaque_cursor(owner, world):
    rows = [propose_task(world, title=f"t{i}") for i in range(3)]
    page = get(owner, "/pending", params={"limit": "2"}).json()
    assert [i["proposal_id"] for i in page["items"]] == [r.proposal_id for r in rows[:2]]
    assert page["next_cursor"]
    rest = get(owner, "/pending", params={"after": page["next_cursor"], "limit": "2"}).json()
    assert [i["proposal_id"] for i in rest["items"]] == [rows[2].proposal_id]
    assert rest["next_cursor"] is None


def test_pending_excludes_decided_and_non_proposal_kinds(owner, world):
    kept = propose_task(world)
    decided = propose_task(world)
    world.decide(ctx(), decided.proposal_id, decided.approval_id, "approve")
    commit_direct(world, ctx(), [task("TAS_direct")])
    body = get(owner, "/pending").json()
    assert [i["proposal_id"] for i in body["items"]] == [kept.proposal_id]
    assert body["observed_version"] == world.version() == 2


@pytest.mark.parametrize(
    "query",
    [
        "limit=0", "limit=201", "limit=abc", "limit=01", "limit=1.5", "limit=-1", "limit=",
        "after=", "after=" + "a" * 513, "after=%20x", "after=a%00b",
        "limit=5&limit=6", "after=a&after=b", "entity=tasks", "since=1.2.3", "x=1",
        "limit", "&", "limit=5&",
    ],
)
def test_pending_query_is_an_exact_allow_list(owner, world, query):
    response = owner.client.get(f"{ROOT}/pending?{query}", headers=owner.login())
    assert response.status_code == 400, (query, response.text)
    assert error(response) == "invalid_query"


def test_a_cursor_from_another_listing_is_refused_by_the_ledger(owner, world):
    response = get(owner, "/pending", params={"after": "fake:not-a-real-cursor"})
    assert response.status_code == 422
    assert error(response) == "invalid_cursor"


# ---------------------------------------------------------------------------------------------
# GET /proposal-requests/{uuid}


def lookup(owner, request_id):
    response = get(owner, f"/proposal-requests/{request_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == REQUEST_KEYS
    assert {k: body[k] for k in BINDING} == BINDING
    assert body["request_id"] == request_id
    return body


def test_an_unknown_request_is_unknown_never_an_error(owner, world):
    body = lookup(owner, UNKNOWN)
    assert (body["status"], body["proposal"], body["receipt"], body["reason_codes"]) == (
        "unknown", None, None, ["proposal_request_unknown"])
    assert body["observed_version"] == world.version()


def test_a_pending_request_is_found_without_a_receipt(owner, world):
    row = propose_task(world)
    body = lookup(owner, row.request_id)
    assert (body["status"], body["receipt"], body["reason_codes"]) == ("found", None, [])
    assert set(body["proposal"]) == PROPOSAL_KEYS
    assert (body["proposal"]["status"], body["proposal"]["proposal_id"]) == (
        "pending", row.proposal_id)


@pytest.mark.parametrize(("decision", "key"), [("approve", "approval_id"),
                                               ("reject", "rejection_id")])
def test_a_decided_request_carries_its_receipt_cross_checked(owner, world, decision, key):
    row = propose_task(world)
    world.decide(ctx(), row.proposal_id, getattr(row, key), decision)
    body = lookup(owner, row.request_id)
    proposal, receipt = body["proposal"], body["receipt"]
    status = "committed" if decision == "approve" else "rejected"
    assert (body["status"], body["reason_codes"]) == ("found", [])
    assert proposal["status"] == receipt["status"] == status
    assert set(receipt) == RECEIPT_KEYS
    assert {k: receipt[k] for k in BINDING} == BINDING
    assert receipt["request_id"] == getattr(row, key)
    assert (receipt["proposal_id"], receipt["actor_source"], receipt["kind"]) == (
        row.proposal_id, "OS", "proposal")
    assert receipt["commit_version"] == 1 and receipt["observed_version"] == 1
    assert receipt["server_now"].endswith("Z")
    assert receipt["undo"] is None and receipt["undo_of"] is None
    if status == "committed":
        assert receipt["committed_at"].endswith("Z")
        assert [r["entity"] for r in receipt["records"]] == ["tasks"]
        assert receipt["records"][0]["record"]["revision"] == 1
    else:
        assert receipt["committed_at"] is None and receipt["records"] == []


def test_an_owner_direct_request_is_found_with_its_undo_view(owner, world):
    proposal, receipt = commit_direct(world, ctx(), [task("TAS_direct")])
    body = lookup(owner, proposal.request_id)
    assert body["proposal"]["kind"] == "owner_direct"
    assert body["proposal"]["actor_source"] == "OS_DIRECT"
    assert body["receipt"]["request_id"] == proposal.request_id == receipt.request_id
    assert body["receipt"]["undo"]["state"] == "available"
    assert body["receipt"]["undo"]["until"].endswith("Z")


@pytest.mark.parametrize(
    ("value", "status", "code"),
    [
        ("not-a-uuid", 422, "invalid_request_id"),
        (UNKNOWN.upper(), 422, "invalid_request_id"),
        (UNKNOWN.replace("-", ""), 422, "invalid_request_id"),
        (UNKNOWN + "?x=1", 400, "invalid_query"),
    ],
)
def test_the_request_id_must_be_a_canonical_uuid_and_no_query_is_accepted(owner, world, value,
                                                                          status, code):
    response = owner.client.get(f"{ROOT}/proposal-requests/{value}", headers=owner.login())
    assert response.status_code == status, response.text
    assert error(response) == code


# ---------------------------------------------------------------------------------------------
# Fail closed (I9): a ledger answer that breaks the §10.2 cross-check is never rendered.


class Tampered:
    """Wraps the fake ledger and corrupts one answer."""

    def __init__(self, ledger, **answers):
        self._ledger, self._answers = ledger, answers

    def __getattr__(self, name):
        if name in self._answers:
            return self._answers[name]
        return getattr(self._ledger, name)


def test_a_decided_proposal_without_its_receipt_is_an_integrity_failure(owner, world,
                                                                        monkeypatch):
    row = propose_task(world)
    world.decide(ctx(), row.proposal_id, row.approval_id, "approve")
    real = world.fake.creation_request(ctx(), row.request_id)
    hidden = replace(real, decision=None)
    tampered = Tampered(world.fake, creation_request=lambda *_a: hidden)
    monkeypatch.setattr(api, "build_ledger", lambda _s: tampered)
    response = get(owner, f"/proposal-requests/{row.request_id}")
    assert response.status_code == 503
    assert error(response) == "ledger_integrity_failed"


def test_a_receipt_under_the_wrong_key_is_an_integrity_failure(owner, world, monkeypatch):
    row = propose_task(world)
    world.decide(ctx(), row.proposal_id, row.rejection_id, "reject")
    real = world.fake.receipt(ctx(), row.rejection_id)
    moved = replace(real, receipt=replace(real.receipt, request_id=row.approval_id))
    tampered = Tampered(world.fake, receipt=lambda *_a: moved)
    monkeypatch.setattr(api, "build_ledger", lambda _s: tampered)
    response = get(owner, f"/proposal-requests/{row.request_id}")
    assert response.status_code == 503
    assert error(response) == "ledger_integrity_failed"


@pytest.mark.parametrize(
    "change",
    [{"epoch": 2}, {"workspace_id": "other"}, {"status": "committed"}, {"kind": "owner_direct"}],
    ids=lambda change: next(iter(change)),
)
def test_a_pending_item_outside_the_binding_is_an_integrity_failure(owner, world, monkeypatch,
                                                                    change):
    propose_task(world)
    page = world.fake.pending(ctx(), after=None, limit=50)
    bad = replace(page, items=(replace(page.items[0], **change),))
    monkeypatch.setattr(api, "build_ledger",
                        lambda _s: Tampered(world.fake, pending=lambda *_a, **_k: bad))
    response = get(owner, "/pending")
    assert response.status_code == 503
    assert error(response) == "ledger_integrity_failed"


def test_reads_never_write(owner, world):
    row = propose_task(world)
    before = (world.version(), len(world.rows("os_proposals")), len(world.rows("os_receipts")))
    get(owner, "/pending")
    lookup(owner, row.request_id)
    lookup(owner, str(uuid.uuid4()))
    assert (world.version(), len(world.rows("os_proposals")),
            len(world.rows("os_receipts"))) == before
