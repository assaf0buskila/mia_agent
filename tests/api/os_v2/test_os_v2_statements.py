"""V2-26 routes (§12.3, §12.4, §9, §10.5): the statement review, draft, approve and cycle views.

The real owner-session machinery runs (cookie, exact Origin, CSRF), over the sqlite session store
of ``tests.unit.test_owner_sessions``. V2-19's real ``app.services.os_ledger.authorize`` runs: its
constructors mint the contexts and ``require`` enforces the §9 matrix. ``AuthorizeSpy`` only wraps
those functions to record which ones a route reached. If the module cannot be imported, every route
fails closed with ``ledger_unavailable``.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime

import pytest
from app.api.os_v2 import statements
from app.api.os_v2.errors import ledger_error_handler
from app.api.owner_os_boundary import OwnerOSBoundary, owner_http_error, owner_validation_error
from app.services.os_ledger import api, authorize
from app.services.os_ledger.statement_tables import tables_for
from app.services.os_ledger.statements import LedgerStatementService, pepper_fingerprint
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from tests.fakes.contexts import make_ctx
from tests.fakes.os_ledger import FakeLedger
from tests.fakes.os_ledger_env import NOW
from tests.services.os_ledger.statements.support import (
    PEPPER,
    Counter,
    batch,
    encode,
    line,
    three_lines,
)
from tests.unit import test_owner_sessions as auth

sessions = auth.sessions
ROOT = "/v1/owner/os/unified"
AUTHORIZE_MODULE = "app.services.os_ledger.authorize"
SCOPES = ",assaf-os:read,assaf-os:propose,assaf-os:approve"


class AuthorizeSpy:
    """Records the V2-19 calls a route makes, around the REAL ``authorize``; replaces nothing."""

    def __init__(self, monkeypatch) -> None:
        self.calls: list[tuple[str, str]] = []
        for name in ("from_owner_mutation", "from_owner_read"):
            monkeypatch.setattr(authorize, name, self._constructor(name, getattr(authorize, name)))
        monkeypatch.setattr(authorize, "require", self._require(authorize.require))

    def _constructor(self, name, real):
        def spy(principal, settings):
            self.calls.append((name, ""))
            return real(principal, settings)

        return spy

    def _require(self, real):
        def spy(ctx, op):
            self.calls.append(("require", op))
            return real(ctx, op)

        return spy


def settings():
    value = auth._settings()
    value.assaf_os_enabled = True
    value.assaf_os_workspace_id = auth.WORKSPACE
    value.owner_os_session_scopes += SCOPES
    value.assaf_os_backend = "postgres"
    value.assaf_os_ledger_enabled = True
    value.assaf_os_authority_epoch = 1
    value.assaf_os_statements_enabled = True
    value.os_statement_pepper = PEPPER.decode("ascii")
    return value


class Backend:
    def __init__(self, config, monkeypatch) -> None:
        self.config = config
        self.ledger = FakeLedger(
            clock=lambda: NOW, uuid4=Counter(0x5000),
            settings_epoch=lambda: config.assaf_os_authority_epoch,
            backend=lambda: config.assaf_os_backend, kill_switch=lambda: config.kill_switch,
            freeze=lambda: config.assaf_os_freeze,
            undo_window_s=lambda: config.owner_undo_window_seconds,
            enabled=lambda: config.assaf_os_ledger_enabled)
        self.ledger.ensure_workspace(auth.WORKSPACE, auth.OWNER_ID, 1)
        for domain in ("core", "statements"):
            self.ledger.transition(auth.WORKSPACE, domain, "active", expected_epoch=1,
                                   actor_owner_id=auth.OWNER_ID)
        self.ledger.pin_statement_pepper(auth.WORKSPACE, pepper_fingerprint(PEPPER))
        self.authorize = AuthorizeSpy(monkeypatch)
        self.service = LedgerStatementService(
            self.ledger, config, pin_reader=lambda tx: tx.data["statement_pepper_fp"])

    def rows(self, table):
        return self.ledger.rows(table)

    def tables(self):
        worker = make_ctx("WORKER", owner=int(auth.OWNER_ID), workspace=auth.WORKSPACE,
                          scopes=frozenset({"retention"}))  # straight to the ledger, no require
        with self.ledger.transaction(worker, domain="statements", kind="statement_batch",
                                     purpose="expire") as tx:
            return tables_for(tx).dump()


def app_for(factory, config):
    app = auth._app(factory, config)
    app.include_router(statements.router)
    app.add_middleware(OwnerOSBoundary)
    app.add_exception_handler(HTTPException, owner_http_error)
    app.add_exception_handler(RequestValidationError, owner_validation_error)
    app.add_exception_handler(api.LedgerError, ledger_error_handler)
    return app


@pytest.fixture
def world(sessions, monkeypatch):
    config = settings()
    state = Backend(config, monkeypatch)
    monkeypatch.setattr(statements, "service_for", lambda _settings: state.service)
    with TestClient(app_for(sessions, config), base_url=auth.ORIGIN) as browser:
        login = auth._login(browser)
        state.browser = browser
        state.sessions_factory = sessions
        state.headers = {"Origin": auth.ORIGIN, "X-CSRF-Token": login["csrf_token"]}
        yield state


def post(world, path, body=None, *, raw=None, headers=None):
    content = raw if raw is not None else json.dumps(body, ensure_ascii=False)
    return world.browser.post(ROOT + path, content=content,
                              headers={"Content-Type": "application/json",
                                       **(world.headers if headers is None else headers)})


def get(world, path):
    return world.browser.get(ROOT + path, headers={"Origin": auth.ORIGIN})


def read(world, path):
    response = get(world, path)
    assert response.status_code == 200, response.text
    return response.json()


def paste(world, lines=None, **kw):
    response = post(world, "/statement-batches", raw=encode(batch(lines or three_lines(), **kw)))
    assert response.status_code == 200, response.text
    return response.json()


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


BINDING = {"api_version": "assaf-os.v2", "source": "postgresql", "workspace_id": auth.WORKSPACE,
           "authority_epoch": 1}


def test_the_owner_flow_end_to_end_through_the_routes(world):
    created = paste(world)
    assert {k: created[k] for k in BINDING} == BINDING
    assert created["status"] == "pending" and created["observed_version"] == 0
    assert created["summary"]["line_count"] == 3
    pid = created["proposal_id"]

    review = read(world, f"/statement-batches/{pid}")
    assert review["status"] == "pending" and review["blocking"] == []
    page = read(world, f"/statement-batches/{pid}/lines?limit=2")
    assert len(page["lines"]) == 2 and page["next_cursor"]
    rest = read(world, f"/statement-batches/{pid}/lines?limit=2&after={page['next_cursor']}")
    assert len(rest["lines"]) == 1 and rest["next_cursor"] is None

    key = page["lines"][0]["line_key"]
    saved = post(world, f"/statement-batches/{pid}/draft", {
        "draft_revision": 0, "overrides": [{"line_key": key, "category": "Tools"}],
        "choices": {}}).json()
    assert saved["draft_revision"] == 1 and saved["observed_version"] == 0

    stale = post(world, f"/statement-batches/{pid}/approve", {
        "request_id": review["approval_request_id"], "review_digest": review["review_digest"]})
    assert_error(stale, 409, "review_stale")
    fresh = read(world, f"/statement-batches/{pid}")
    approved = post(world, f"/statement-batches/{pid}/approve", {
        "request_id": fresh["approval_request_id"], "review_digest": fresh["review_digest"]})
    assert approved.status_code == 200, approved.text
    receipt = approved.json()
    assert {k: receipt[k] for k in BINDING} == BINDING
    assert (receipt["status"], receipt["kind"], receipt["actor_source"], receipt["undo"]) == (
        "committed", "statement_batch", "ASSISTANT_PASTE", None)
    assert receipt["observed_version"] == receipt["commit_version"] == 1
    assert [r["entity"] for r in receipt["records"]] == ["expenses"]
    assert datetime.fromisoformat(receipt["server_now"].replace("Z", "+00:00")).tzinfo is UTC

    cycles = read(world, "/statements")
    assert len(cycles["cycles"]) == 36 and cycles["cycles"][0]["status"] == "imported"
    statement_id = cycles["cycles"][0]["statement_id"]
    view = read(world, f"/statements/{statement_id}")
    assert view["totals"]["business_line_count"] == 2 and view["previous"] is None
    assert read(world, "/merchant-rules")["rules"] == []


def test_reject_returns_a_receipt_and_leaves_no_detail(world):
    created = paste(world)
    pid = created["proposal_id"]
    review = read(world, f"/statement-batches/{pid}")
    response = post(world, f"/statement-batches/{pid}/reject",
                    {"request_id": review["rejection_request_id"]})
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected" and response.json()["records"] == []
    assert all(r.detail is None for r in world.tables()["batch_lines"].values())
    again = post(world, f"/statement-batches/{pid}/reject",
                 {"request_id": review["rejection_request_id"]})
    assert again.status_code == 200 and again.json()["request_id"] == review["rejection_request_id"]


ALL_ROUTES = [
    ("GET", "/statements", None),
    ("GET", "/statements/STM_" + "0" * 32, None),
    ("POST", "/statement-batches", b"{}"),
    ("GET", "/statement-batches/PRO_" + "0" * 32, None),
    ("GET", "/statement-batches/PRO_" + "0" * 32 + "/lines", None),
    ("POST", "/statement-batches/PRO_" + "0" * 32 + "/draft", b"{}"),
    ("POST", "/statement-batches/PRO_" + "0" * 32 + "/approve", b"{}"),
    ("POST", "/statement-batches/PRO_" + "0" * 32 + "/reject", b"{}"),
    ("GET", "/merchant-rules", None),
]


def call(world, method, path, body, headers=None):
    if method == "GET":
        return get(world, path)
    return post(world, path, raw=body, headers=headers)


@pytest.mark.parametrize("method,path,body", ALL_ROUTES)
def test_a_disabled_flag_or_the_sheets_backend_is_a_404_before_auth(world, method, path, body):
    for knob, value in (("assaf_os_statements_enabled", False), ("assaf_os_backend", "sheets")):
        setattr(world.config, knob, value)
        response = call(world, method, path, body, headers={})  # not even a CSRF header
        assert_error(response, 404, "operation_unavailable", write=method == "POST")
        setattr(world.config, knob, True if knob.endswith("enabled") else "postgres")
    assert world.authorize.calls == []


@pytest.mark.parametrize("method,path,body", ALL_ROUTES)
def test_an_authorization_header_is_refused_on_every_route(world, method, path, body):
    headers = {**world.headers, "Authorization": "Bearer aosp_synthetic"}
    if method == "GET":
        response = world.browser.get(ROOT + path, headers={
            "Origin": auth.ORIGIN, "Authorization": "Bearer x"})
    else:
        response = post(world, path, raw=body, headers=headers)
    assert_error(response, 401, "owner_auth_required", write=method == "POST")
    assert world.authorize.calls == []


@pytest.mark.parametrize("method,path,body", [r for r in ALL_ROUTES if r[0] == "POST"])
def test_a_write_needs_the_origin_and_the_csrf_token(world, method, path, body):
    no_csrf = {"Origin": auth.ORIGIN}
    no_origin = {"X-CSRF-Token": world.headers["X-CSRF-Token"]}
    wrong_origin = {**world.headers, "Origin": "https://evil.example"}
    for headers in (no_csrf, no_origin, wrong_origin):
        response = post(world, path, raw=body, headers=headers)
        assert response.status_code == 403, response.text
    assert world.rows("os_proposals") == [] and world.authorize.calls == []


@pytest.mark.parametrize("method,path,body", ALL_ROUTES)
def test_no_session_is_401(world, method, path, body):
    with TestClient(app_for(world.sessions_factory, world.config), base_url=auth.ORIGIN) as other:
        response = (other.get(ROOT + path, headers={"Origin": auth.ORIGIN}) if method == "GET"
                    else other.post(ROOT + path, content=body, headers={
                        "Content-Type": "application/json", "Origin": auth.ORIGIN}))
    assert response.status_code == 401, response.text


def test_the_routes_fail_closed_until_v2_19_exists(world, monkeypatch):
    monkeypatch.setitem(sys.modules, AUTHORIZE_MODULE, None)  # an import of it now fails
    response = post(world, "/statement-batches", raw=encode(batch(three_lines())))
    assert_error(response, 503, "ledger_unavailable")
    assert world.rows("os_proposals") == []


def test_the_reads_use_a_read_context_and_the_writes_a_mutation_context(world):
    created = paste(world)
    get(world, f"/statement-batches/{created['proposal_id']}")
    assert ("from_owner_mutation", "") in world.authorize.calls
    assert ("from_owner_read", "") in world.authorize.calls
    ops = [op for kind, op in world.authorize.calls if kind == "require"]
    assert ops == ["propose_statement_batch", "read_own_batches"]


def test_a_paste_invalid_for_the_domain_is_a_422_with_codes_only(world):
    response = post(world, "/statement-batches", raw=b'{"schema": "nope"}')
    error = assert_error(response, 422, "invalid_statement_batch")
    assert error["details"]["issues"] and all(
        set(i) == {"field", "code"} for i in error["details"]["issues"])
    assert world.rows("os_proposals") == []


@pytest.mark.parametrize("path,body", [
    ("/draft", {"draft_revision": 0}),
    ("/draft", {"draft_revision": 0, "overrides": [], "extra": 1}),
    ("/approve", {"request_id": "x"}),
    ("/approve", {"request_id": "x", "review_digest": "y", "more": 1}),
    ("/reject", {}),
    ("/reject", {"request_id": "x", "extra": True}),
])
def test_bodies_are_strict(world, path, body):
    pid = paste(world)["proposal_id"]
    response = post(world, f"/statement-batches/{pid}{path}", body)
    assert_error(response, 422, "validation_error", details={"reason": "invalid_body"})


def test_duplicate_keys_and_bad_json_are_refused(world):
    pid = paste(world)["proposal_id"]
    for raw in (b'{"request_id": "a", "request_id": "b"}', b"not json", b"[]"):
        response = post(world, f"/statement-batches/{pid}/reject", raw=raw)
        assert response.status_code in {400, 422}, response.text


def test_a_non_string_request_id_is_invalid(world):
    pid = paste(world)["proposal_id"]
    response = post(world, f"/statement-batches/{pid}/reject", {"request_id": 5})
    assert_error(response, 422, "invalid_request_id")


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "limit=x", "limit=1&limit=2",
                                   "since=1", "after=", "limit=-1"])
def test_the_lines_query_is_an_allow_list(world, query):
    pid = paste(world)["proposal_id"]
    response = get(world, f"/statement-batches/{pid}/lines?{query}")
    assert response.status_code in {400, 422}, response.text


def test_an_unknown_batch_or_statement_is_404(world):
    for path in ("/statement-batches/PRO_" + "1" * 32, "/statement-batches/nope",
                 "/statements/STM_" + "1" * 32):
        assert_error(get(world, path), 404, "batch_not_found", write=False)


def test_the_kill_switch_blocks_a_write_and_the_error_says_nothing_started(world):
    world.config.kill_switch = True
    response = post(world, "/statement-batches", raw=encode(batch(three_lines())))
    assert_error(response, 403, "kill_switch")
    assert world.rows("os_proposals") == []


def test_responses_are_never_cached(world):
    pid = paste(world)["proposal_id"]
    assert get(world, f"/statement-batches/{pid}").headers["cache-control"] == "private, no-store"


def test_a_statement_text_never_reaches_an_error_envelope(world):
    paste(world, [line("PERSONAL CAFE", 421731, tag="personal"), line("DEMO SHOP", 10000)])
    response = post(world, "/statement-batches", raw=encode(
        batch([line("PERSONAL CAFE", 421731, tag="personal")], number=1)))
    assert "PERSONAL CAFE" not in response.text and "421731" not in response.text
