"""V2-24 routes (§11.1, §9, §10.5): POST /commits, /commits/{id}/undo|abandon and GET /deleted.

The real owner-session machinery runs (cookie, exact Origin, CSRF), over the sqlite session store
of ``tests.unit.test_owner_sessions``. Two V2-19 pieces are not on this base and are stood in:
``app.services.os_ledger.authorize`` (through ``sys.modules``, as V2-28 does) and the
``assaf-os:direct`` scope in ``owner_sessions``' allow-lists. Without them every route fails closed.
"""

from __future__ import annotations

import ast
import json
import sys
import types
from pathlib import Path

import pytest
from app.api import owner_sessions
from app.api.os_v2 import commits
from app.api.os_v2.errors import ledger_error_handler
from app.api.owner_os_boundary import OwnerOSBoundary, owner_http_error, owner_validation_error
from app.services.os_ledger import api
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from tests.fakes.contexts import make_ctx
from tests.fakes.os_ledger import FakeLedger
from tests.fakes.os_ledger_builders import commit_direct, task
from tests.fakes.os_ledger_env import NOW, Knobs, LedgerEnv
from tests.unit import test_owner_sessions as auth

sessions = auth.sessions
ROOT = "/v1/owner/os/unified"
AUTHORIZE_MODULE = "app.services.os_ledger.authorize"
DIRECT = "assaf-os:direct"
TASK_OPEN = "לביצוע"
REPO = Path(__file__).resolve().parents[3]


def uuid4_of(number: int) -> str:
    return f"00000000-0000-4000-8000-{number:012d}"


class FakeAuthorize(types.ModuleType):
    """Stand-in for V2-19's ``authorize``: §9's constructors and ``require`` for these ops."""

    def __init__(self) -> None:
        super().__init__(AUTHORIZE_MODULE)
        self.channel = "OS"
        self.calls: list[tuple[str, str]] = []

    @staticmethod
    def _owner(principal, settings) -> int:
        owner = str(getattr(principal, "owner_id", ""))
        if not owner.isdigit() or owner not in settings.telegram_owner_user_id_set():
            raise api.LedgerError("owner_auth_required")
        return int(owner)

    def _ctx(self, principal, settings, *, verified: bool):
        return make_ctx(self.channel, owner=self._owner(principal, settings),
                        workspace=settings.assaf_os_workspace_id,
                        epoch=settings.assaf_os_authority_epoch, scopes=principal.scopes,
                        browser_mutation_verified=verified)

    def from_owner_mutation(self, principal, settings):
        self.calls.append(("from_owner_mutation", ""))
        return self._ctx(principal, settings, verified=True)

    def from_owner_read(self, principal, settings):
        self.calls.append(("from_owner_read", ""))
        return self._ctx(principal, settings, verified=False)

    def require(self, ctx, op):
        self.calls.append(("require", op))
        if op in {"commit_direct", "undo", "abandon"}:
            if ctx.channel != "OS":
                raise api.LedgerError("channel_operation_denied")
            if DIRECT not in ctx.scopes:
                raise api.LedgerError("scope_denied")
            if not ctx.browser_mutation_verified:
                raise api.LedgerError("browser_mutation_verification_required")
        elif op != "read" or "assaf-os:read" not in ctx.scopes:
            raise api.LedgerError("scope_denied")


def settings():
    value = auth._settings()
    value.assaf_os_enabled = True
    value.assaf_os_workspace_id = auth.WORKSPACE
    value.owner_os_session_scopes += ",assaf-os:read," + DIRECT
    value.assaf_os_backend = "postgres"
    value.assaf_os_ledger_enabled = True
    value.assaf_os_authority_epoch = 1
    value.assaf_os_owner_direct_enabled = True
    return value


class World:
    def __init__(self, config) -> None:
        self.config = config
        self.ledger = FakeLedger(
            clock=lambda: NOW,
            settings_epoch=lambda: config.assaf_os_authority_epoch,
            backend=lambda: config.assaf_os_backend,
            kill_switch=lambda: config.kill_switch,
            freeze=lambda: config.assaf_os_freeze,
            undo_window_s=lambda: config.owner_undo_window_seconds,
            enabled=lambda: config.assaf_os_ledger_enabled,
        )
        self.ledger.ensure_workspace(auth.WORKSPACE, auth.OWNER_ID, 1)
        self.ledger.transition(auth.WORKSPACE, "core", "active", expected_epoch=1,
                               actor_owner_id=auth.OWNER_ID)
        self.authorize = FakeAuthorize()
        # The LedgerTx-level seeding helpers need a LedgerEnv (they read knobs.undo_window_s).
        self.env = LedgerEnv("fake", self.ledger, Knobs(), rows=self.ledger.rows,
                             version=self.ledger.version, now=self.ledger._now,
                             advance=self.ledger.advance)

    def seed(self, mutations):
        commit_direct(self.env, self.ctx(), mutations)

    def ctx(self):
        return make_ctx("OS", owner=int(auth.OWNER_ID), workspace=auth.WORKSPACE)

    def version(self):
        return self.ledger.version(auth.WORKSPACE)

    def rows(self, table):
        return self.ledger.rows(table)


def app_for(factory, config):
    app = auth._app(factory, config)
    app.include_router(commits.router)
    app.add_middleware(OwnerOSBoundary)
    app.add_exception_handler(HTTPException, owner_http_error)
    app.add_exception_handler(RequestValidationError, owner_validation_error)
    app.add_exception_handler(api.LedgerError, ledger_error_handler)
    return app


@pytest.fixture
def world(sessions, monkeypatch):
    config = settings()
    state = World(config)
    monkeypatch.setattr(api, "build_ledger", lambda _settings: state.ledger)
    monkeypatch.setitem(sys.modules, AUTHORIZE_MODULE, state.authorize)
    monkeypatch.setattr(owner_sessions, "ASSAF_OS_WRITE_SCOPES",
                        owner_sessions.ASSAF_OS_WRITE_SCOPES | {DIRECT})
    monkeypatch.setattr(owner_sessions, "KNOWN_SCOPES", owner_sessions.KNOWN_SCOPES | {DIRECT})
    with TestClient(app_for(sessions, config), base_url=auth.ORIGIN) as browser:
        login = auth._login(browser)
        state.browser = browser
        state.headers = {"Origin": auth.ORIGIN, "X-CSRF-Token": login["csrf_token"]}
        yield state


def post(world, path, body, *, headers=None):
    return world.browser.post(ROOT + path, content=json.dumps(body, ensure_ascii=False),
                              headers={"Content-Type": "application/json",
                                       **(world.headers if headers is None else headers)})


def create_body(request_id=None, **patch):
    return {"request_id": request_id or uuid4_of(1), "op": "create", "entity": "tasks",
            "record_id": None, "expected_revision": 0,
            "patch": patch or {"title": "Task", "status": TASK_OPEN}}


def assert_error(response, status, code, *, write=True, details=None):
    assert response.status_code == status, response.text
    envelope = response.json()
    assert set(envelope) == {"api_version", "request_id", "error"}
    error = envelope["error"]
    assert error["code"] == code
    if write:
        assert error.get("write_outcome") == "not_started"
    if details is not None:
        assert error["details"] == details
    return error


ROUTES = [
    ("POST", "/commits"),
    ("POST", f"/commits/{uuid4_of(1)}/undo"),
    ("POST", f"/commits/{uuid4_of(1)}/abandon"),
    ("GET", "/deleted"),
]


def call(world, method, path, *, headers=None):
    if method == "GET":
        return world.browser.get(ROOT + path, headers=world.headers if headers is None
                                 else headers)
    body = create_body() if path == "/commits" else (
        {"undo_request_id": uuid4_of(2)} if path.endswith("/undo") else {})
    return post(world, path, body, headers=headers)


# --- the whole route is guarded --------------------------------------------------------------


@pytest.mark.parametrize("method,path", ROUTES)
@pytest.mark.parametrize("toggle", ["flag_off", "sheets"])
def test_every_route_is_404_when_owner_direct_is_off_or_the_backend_is_not_postgres(
        world, method, path, toggle):
    if toggle == "flag_off":
        world.config.assaf_os_owner_direct_enabled = False
    else:
        world.config.assaf_os_backend = "sheets"
    response = call(world, method, path)
    assert_error(response, 404, "operation_unavailable", write=method == "POST")
    assert world.version() == 0 and world.authorize.calls == []


@pytest.mark.parametrize("method,path", ROUTES)
def test_a_bearer_request_is_refused(world, method, path):
    response = call(world, method, path,
                    headers=world.headers | {"Authorization": "Bearer token"})
    assert_error(response, 401, "owner_auth_required", write=method == "POST")
    assert world.version() == 0 and world.authorize.calls == []


@pytest.mark.parametrize("method,path", ROUTES)
def test_without_a_session_the_owner_is_unauthenticated(world, method, path):
    world.browser.cookies.clear()
    response = call(world, method, path)
    assert response.status_code == 401 and response.json()["error"]["code"] == (
        "owner_auth_required")
    assert world.version() == 0


@pytest.mark.parametrize("path", [p for m, p in ROUTES if m == "POST"])
def test_writes_need_the_csrf_token_and_the_exact_origin(world, path):
    no_csrf = call(world, "POST", path, headers={"Origin": auth.ORIGIN})
    assert (no_csrf.status_code, no_csrf.json()["error"]["code"]) == (403, "csrf_denied")
    wrong_origin = call(world, "POST", path,
                        headers=world.headers | {"Origin": "https://evil.example"})
    assert (wrong_origin.status_code, wrong_origin.json()["error"]["code"]) == (
        403, "origin_denied")
    assert world.version() == 0 and world.authorize.calls == []


def test_a_session_without_the_direct_scope_is_refused(world):
    world.config.owner_os_session_scopes = world.config.owner_os_session_scopes.replace(
        "," + DIRECT, "")
    response = post(world, "/commits", create_body())
    assert (response.status_code, response.json()["error"]["code"]) == (
        403, "owner_scope_denied")
    assert world.version() == 0


def test_before_v2_19_adds_the_direct_scope_the_routes_fail_closed(world, monkeypatch):
    monkeypatch.setattr(owner_sessions, "ASSAF_OS_WRITE_SCOPES",
                        owner_sessions.ASSAF_OS_WRITE_SCOPES - {DIRECT})
    response = post(world, "/commits", create_body())
    assert_error(response, 503, "ledger_unavailable")
    assert world.version() == 0 and world.authorize.calls == []


@pytest.mark.parametrize("method,path", ROUTES)
def test_without_v2_19_authorize_every_route_fails_closed(world, monkeypatch, method, path):
    monkeypatch.setitem(sys.modules, AUTHORIZE_MODULE, None)
    response = call(world, method, path)
    assert_error(response, 503, "ledger_unavailable", write=method == "POST")
    assert world.version() == 0


@pytest.mark.parametrize("channel", ["MIA", "TELEGRAM_APPROVAL", "ASSISTANT_CLAUDE",
                                     "ASSISTANT_GPT"])
def test_a_non_os_context_is_refused_even_past_authorize(world, monkeypatch, channel):
    world.authorize.channel = channel
    monkeypatch.setattr(world.authorize, "require", lambda ctx, op: None)
    for path in ("/commits", f"/commits/{uuid4_of(1)}/undo", f"/commits/{uuid4_of(1)}/abandon"):
        response = call(world, "POST", path)
        assert_error(response, 403, "channel_operation_denied")
    assert world.version() == 0


def test_the_kill_switch_blocks_every_write_route(world):
    world.config.kill_switch = True
    for path in ("/commits", f"/commits/{uuid4_of(1)}/undo", f"/commits/{uuid4_of(1)}/abandon"):
        assert_error(call(world, "POST", path), 403, "kill_switch")
    assert world.version() == 0


@pytest.mark.parametrize("path", [p for m, p in ROUTES if m == "POST"])
def test_each_write_route_caps_its_body_at_16_kib(world, path):
    body = {"pad": "x" * 16_400}
    response = post(world, path, body)
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "validation_error"
    assert world.version() == 0


# --- the paths ---------------------------------------------------------------------------------


def test_commit_undo_and_abandon_return_receipt_v2(world):
    created = post(world, "/commits", create_body(uuid4_of(11)))
    assert created.status_code == 200, created.text
    body = created.json()
    assert (body["status"], body["kind"], body["actor_source"]) == (
        "committed", "owner_direct", "OS_DIRECT")
    assert (body["api_version"], body["source"], body["workspace_id"], body["authority_epoch"]) == (
        "assaf-os.v2", "postgresql", auth.WORKSPACE, 1)
    assert body["observed_version"] == 1 and body["undo"]["state"] == "available"
    assert created.headers["cache-control"] == "private, no-store"
    replay = post(world, "/commits", create_body(uuid4_of(11)))
    assert replay.json() == body
    undone = post(world, f"/commits/{uuid4_of(11)}/undo", {"undo_request_id": uuid4_of(12)})
    assert undone.status_code == 200 and undone.json()["undo_of"] == uuid4_of(11)
    abandoned = post(world, f"/commits/{uuid4_of(13)}/abandon", {})
    assert abandoned.status_code == 200 and abandoned.json()["status"] == "abandoned"
    assert [c for c in world.authorize.calls if c[0] == "require"] == [
        ("require", "commit_direct"), ("require", "commit_direct"), ("require", "undo"),
        ("require", "abandon")]
    assert world.version() == 3


def test_abandon_accepts_an_empty_body(world):
    response = world.browser.post(ROOT + f"/commits/{uuid4_of(21)}/abandon",
                                  headers=world.headers)
    assert response.status_code == 200 and response.json()["status"] == "abandoned"


def test_a_target_changed_commit_is_a_200_receipt(world):
    world.seed([task("TAS_t1")])
    stale = {"request_id": uuid4_of(31), "op": "complete", "entity": "tasks",
             "record_id": "TAS_t1", "expected_revision": 7, "patch": {}}
    response = post(world, "/commits", stale)
    assert response.status_code == 200 and response.json()["status"] == "target_changed"


def test_the_source_is_assigned_by_the_server_and_never_read_from_the_body(world):
    response = post(world, "/commits", create_body() | {"source": "MIA"})
    assert_error(response, 422, "validation_error", details={"reason": "invalid_body"})
    assert world.version() == 0
    ok = post(world, "/commits", create_body(uuid4_of(41)))
    task_id = ok.json()["records"][0]["record"]["task_id"]
    row = next(r for r in world.rows("os_tasks") if r["record_id"] == task_id)
    proposal = next(p for p in world.rows("os_proposals") if p["request_id"] == uuid4_of(41))
    assert row["source"] == proposal["source"] == "OS_DIRECT"


@pytest.mark.parametrize("raw,status", [("[]", 422), ("{\"a\":1,\"a\":2}", 422),
                                        ("not json", 422)])
def test_a_body_that_is_not_one_json_object_is_refused(world, raw, status):
    response = world.browser.post(ROOT + "/commits", content=raw,
                                  headers={"Content-Type": "application/json", **world.headers})
    assert response.status_code == status
    assert world.version() == 0


def test_an_error_carries_only_allowed_details(world):
    world.seed([task("TAS_t1")])
    post(world, "/commits", {"request_id": uuid4_of(51), "op": "complete", "entity": "tasks",
                             "record_id": "TAS_t1", "expected_revision": 1, "patch": {}})
    post(world, f"/commits/{uuid4_of(51)}/undo", {"undo_request_id": uuid4_of(52)})
    response = post(world, f"/commits/{uuid4_of(51)}/undo", {"undo_request_id": uuid4_of(53)})
    assert_error(response, 409, "already_undone", details={"undo_request_id": uuid4_of(52)})


# --- GET /deleted ------------------------------------------------------------------------------


def test_deleted_lists_tombstoned_records_with_the_binding(world):
    world.seed([task("TAS_t1"), task("TAS_t2")])
    post(world, "/commits", {"request_id": uuid4_of(61), "op": "delete", "entity": "tasks",
                             "record_id": "TAS_t2", "expected_revision": 1, "patch": {}})
    response = world.browser.get(ROOT + "/deleted", headers=world.headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"api_version", "source", "workspace_id", "authority_epoch",
                         "observed_version", "entity", "records", "next_cursor"}
    assert (body["entity"], body["next_cursor"], body["observed_version"]) == ("tasks", None, 2)
    assert [r["task_id"] for r in body["records"]] == ["TAS_t2"]
    assert ("require", "read") in world.authorize.calls


@pytest.mark.parametrize("query", [
    "?since=1.0.0", "?entity=tasks&entity=tasks", "?entity=proposals", "?limit=0",
    "?limit=201", "?limit=01", "?limit=x", "?after=", "?after=" + "a" * 513, "?foo=1",
])
def test_deleted_refuses_a_query_outside_the_allow_list(world, query):
    response = world.browser.get(ROOT + "/deleted" + query, headers=world.headers)
    assert_error(response, 400, "invalid_query", write=False)


def test_deleted_pages_with_an_opaque_cursor(world):
    world.seed([task(f"TAS_p{i}") for i in range(3)])
    for number, record_id in enumerate(("TAS_p0", "TAS_p1", "TAS_p2"), start=71):
        post(world, "/commits", {"request_id": uuid4_of(number), "op": "delete",
                                 "entity": "tasks", "record_id": record_id,
                                 "expected_revision": 1, "patch": {}})
    first = world.browser.get(ROOT + "/deleted?entity=tasks&limit=2", headers=world.headers)
    cursor = first.json()["next_cursor"]
    assert len(first.json()["records"]) == 2 and cursor
    second = world.browser.get(ROOT + "/deleted", params={"after": cursor, "limit": "2"},
                               headers=world.headers)
    assert len(second.json()["records"]) == 1 and second.json()["next_cursor"] is None


# --- reachability ------------------------------------------------------------------------------


def test_only_the_commits_router_calls_into_owner_direct():
    """Nothing but ``app/api/os_v2/commits.py`` imports the service (§9 allow-list)."""
    offenders = []
    for path in (REPO / "app").rglob("*.py"):
        relative = path.relative_to(REPO).as_posix()
        if relative in {"app/api/os_v2/commits.py", "app/services/os_ledger/owner_direct.py"}:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.ImportFrom):
                names = [node.module or ""] + [f"{node.module}.{a.name}" for a in node.names]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            if any(n.endswith("os_ledger.owner_direct") for n in names):
                offenders.append(relative)
    assert offenders == []


def test_every_commits_route_is_guarded_then_needs_an_owner_session():
    routes = {(next(iter(r.methods)), r.path): r for r in commits.router.routes}
    assert set(routes) == {
        ("POST", ROOT + "/commits"), ("POST", ROOT + "/commits/{request_id}/undo"),
        ("POST", ROOT + "/commits/{request_id}/abandon"), ("GET", ROOT + "/deleted")}
    for (method, _), route in routes.items():
        calls = [d.call for d in route.dependant.dependencies]
        assert calls[0] is commits.route_guard
        owner = commits.direct_principal if method == "POST" else commits.read_principal
        assert owner in calls
