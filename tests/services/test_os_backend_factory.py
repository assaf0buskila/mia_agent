"""V2-17: the OS backend factory and the two-sided authority fence (V2_PG_CONTRACTS.md section 7).

Covers, in order: the factory matrix, the Sheets wrapper, the Sheets-side fence driven through the
real v1 routes (every refusal must leave ``write_outcome:"not_started"``), the postgres snapshot
query allow-list, the v1 golden comparison and the AST grep that nothing else constructs the port.
"""

from __future__ import annotations

import ast
import json
import logging
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from app.api import assaf_os as api
from app.api.os_v2.errors import ledger_error_handler
from app.api.owner_os_boundary import OwnerOSBoundary, owner_http_error, owner_validation_error
from app.core import config as settings_module
from app.core.config import Settings
from app.services import assaf_os as port
from app.services import assaf_os_snapshot_cache as cache
from app.services.os_backend import (
    SheetsOsBackend,
    SnapshotQuery,
    get_os_backend,
    parse_snapshot_query,
)
from app.services.os_ledger import api as ledger_api
from app.services.os_ledger.api import LedgerError
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException

from tests.unit import test_assaf_os as assaf
from tests.unit import test_owner_sessions as auth

sessions = auth.sessions
OWNER = auth.OWNER_ID
ROOT = "/v1/owner/os/unified"
OPS = ("propose", "approve", "reject")
PG_MODULE = "app.services.os_backend_pg"
SENTINEL = "PRIVATE payload must never be logged"
LOGGER = "mia.os_backend"
APP_ROOT = Path(__file__).resolve().parents[2] / "app"


# --------------------------------------------------------------------------- helpers


def pg_settings(**changes):
    config = assaf.settings()
    config.assaf_os_backend = "postgres"
    config.assaf_os_ledger_enabled = True
    config.assaf_os_authority_epoch = 1
    for key, value in changes.items():
        setattr(config, key, value)
    return config


def backend_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


class StubPostgresBackend:
    """Stands in for V2-20's ``app.services.os_backend_pg.PostgresOsBackend``."""

    kind = "postgres"
    instances: list = []
    queries: list = []
    failures: dict = {}

    def __init__(self, settings, owner_id, *, actor, principal=None):
        type(self).instances.append(self)
        self.args = (settings, owner_id, actor, principal)

    def _maybe_fail(self, name):
        failure = type(self).failures.get(name)
        if failure is not None:
            raise failure

    def schema(self):
        self._maybe_fail("schema")
        return {"stub": "schema"}

    def snapshot(self, query=None):
        self._maybe_fail("snapshot")
        type(self).queries.append(query)
        return {"stub": "snapshot", "entity": query.entity if query else None}

    def propose(self, request):
        self._maybe_fail("propose")
        return {"stub": "propose"}

    def proposal(self, proposal_id):
        self._maybe_fail("proposal")
        return {"stub": "proposal"}

    def decide(self, operation, proposal_id, request):
        self._maybe_fail("decide")
        return {"stub": operation}

    def receipt(self, request_id):
        self._maybe_fail("receipt")
        return {"stub": "receipt"}

    def proposal_request(self, request_id):
        return {"stub": "proposal_request"}


@pytest.fixture
def pg_stub(monkeypatch):
    backend = type("StubPostgresBackend", (StubPostgresBackend,), {})
    backend.instances, backend.queries, backend.failures = [], [], {}
    module = types.ModuleType(PG_MODULE)
    module.PostgresOsBackend = backend
    monkeypatch.setitem(sys.modules, PG_MODULE, module)
    return backend


@pytest.fixture
def pg_missing(monkeypatch):
    # None in sys.modules makes the import fail whether or not V2-20 has merged.
    monkeypatch.setitem(sys.modules, PG_MODULE, None)


@pytest.fixture(autouse=True)
def clean_process_caches():
    port._clear_assaf_os_decision_binding_cache()
    port._clear_assaf_os_schema_cache()
    yield
    port._clear_assaf_os_decision_binding_cache()
    port._clear_assaf_os_schema_cache()


# --------------------------------------------------------------------------- factory matrix


def test_sheets_returns_the_wrapped_port_and_never_touches_the_ledger(monkeypatch, caplog):
    config = assaf.settings()
    config.assaf_os_ledger_enabled = True
    config.assaf_os_authority_epoch = 7
    ledger_calls = []
    monkeypatch.setattr(ledger_api, "core_authority_state", lambda s: ledger_calls.append(s))
    monkeypatch.setattr(ledger_api, "build_ledger", lambda s: ledger_calls.append(s))
    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        backend = get_os_backend(config, OWNER, actor="MIA")
    assert isinstance(backend, SheetsOsBackend) and backend.kind == "sheets"
    assert isinstance(backend._port, port.AssafOsPort)
    assert (backend.owner_id, backend.actor_source) == (OWNER, "MIA")
    assert ledger_calls == []
    assert backend_lines(caplog) == ["os_backend op=select outcome=sheets reason=ok"]
    assert [r.levelno for r in caplog.records if r.name == LOGGER] == [logging.DEBUG]


def test_sheets_never_returns_a_ledger_backend_even_with_the_ledger_ready(pg_stub):
    config = pg_settings(assaf_os_backend="sheets")
    for actor in ("OS", "MIA", "TELEGRAM_APPROVAL"):
        backend = get_os_backend(config, OWNER, actor=actor, principal=object())
        assert backend.kind == "sheets" and not isinstance(backend, pg_stub)
    assert pg_stub.instances == []


def test_the_port_class_is_read_from_its_module_at_call_time(monkeypatch):
    built = []

    class Recorder:
        def __init__(self, settings, owner_id, *, actor_source):
            built.append((settings, owner_id, actor_source))

    monkeypatch.setattr(port, "AssafOsPort", Recorder)
    config = assaf.settings()
    backend = get_os_backend(config, OWNER, actor="TELEGRAM_APPROVAL")
    assert built == [(config, OWNER, "TELEGRAM_APPROVAL")]
    assert isinstance(backend._port, Recorder)


@pytest.mark.parametrize(
    "mutate,reason",
    [
        (lambda c: setattr(c, "assaf_os_ledger_enabled", False), "ledger_disabled"),
        (lambda c: setattr(c, "assaf_os_authority_epoch", 0), "epoch_zero"),
    ],
)
def test_postgres_fails_closed_when_the_ledger_is_off_or_the_epoch_is_zero(
    pg_stub, mutate, reason, caplog
):
    config = pg_settings()
    mutate(config)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with pytest.raises(port.AssafOsError) as refused:
            get_os_backend(config, OWNER, actor="OS")
    assert (refused.value.code, refused.value.status) == ("ledger_unavailable", 503)
    assert pg_stub.instances == [], "the ledger backend is never built on a refusal"
    assert backend_lines(caplog) == [f"os_backend op=select outcome=refused reason={reason}"]


def test_postgres_fails_closed_when_the_module_is_missing(pg_missing, caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with pytest.raises(port.AssafOsError) as refused:
            get_os_backend(pg_settings(), OWNER, actor="OS")
    assert (refused.value.code, refused.value.status) == ("ledger_unavailable", 503)
    assert backend_lines(caplog) == ["os_backend op=select outcome=refused reason=module_missing"]


def test_postgres_fails_closed_when_the_module_has_no_backend_class(monkeypatch, caplog):
    monkeypatch.setitem(sys.modules, PG_MODULE, types.ModuleType(PG_MODULE))
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with pytest.raises(port.AssafOsError) as refused:
            get_os_backend(pg_settings(), OWNER, actor="OS")
    assert (refused.value.code, refused.value.status) == ("ledger_unavailable", 503)
    assert backend_lines(caplog) == ["os_backend op=select outcome=refused reason=module_missing"]


def test_postgres_fails_closed_when_the_backend_constructor_raises(monkeypatch, caplog):
    module = types.ModuleType(PG_MODULE)

    class Broken:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("secret dsn text")

    module.PostgresOsBackend = Broken
    monkeypatch.setitem(sys.modules, PG_MODULE, module)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with pytest.raises(port.AssafOsError) as refused:
            get_os_backend(pg_settings(), OWNER, actor="OS")
    assert (refused.value.code, refused.value.status) == ("ledger_unavailable", 503)
    assert refused.value.__cause__ is None and refused.value.__suppress_context__
    lines = backend_lines(caplog)
    assert lines == ["os_backend op=select outcome=refused reason=backend_init_failed"]
    assert all("secret" not in record.getMessage() for record in caplog.records)


def test_postgres_never_builds_the_sheets_port(pg_stub, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("the Sheets port must not be built under postgres")

    monkeypatch.setattr(port, "AssafOsPort", forbidden)
    get_os_backend(pg_settings(), OWNER, actor="OS")


def test_postgres_returns_the_stub_with_the_exact_constructor_arguments(pg_stub, caplog):
    config, principal = pg_settings(), SimpleNamespace(owner_id=OWNER)
    with caplog.at_level(logging.INFO, logger=LOGGER):
        backend = get_os_backend(config, OWNER, actor="OS", principal=principal)
    assert backend is pg_stub.instances[0] and backend.kind == "postgres"
    assert backend.args[0] is config and backend.args[1:3] == (OWNER, "OS")
    assert backend.args[3] is principal
    assert backend_lines(caplog) == ["os_backend op=select outcome=postgres reason=ok"]
    assert get_os_backend(config, OWNER, actor="MIA").args[3] is None


def test_a_refusal_raised_by_the_ledger_backend_propagates(pg_stub, caplog):
    def refuse(self, settings, owner_id, *, actor, principal=None):
        raise LedgerError("ledger_unavailable", reason="role")

    pg_stub.__init__ = refuse
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with pytest.raises(LedgerError) as refused:
            get_os_backend(pg_settings(), OWNER, actor="OS")
    assert refused.value.code == "ledger_unavailable"
    assert backend_lines(caplog) == [
        "os_backend op=select outcome=refused reason=ledger_unavailable"
    ]


def test_an_invalid_backend_value_coerces_to_sheets(monkeypatch):
    monkeypatch.setattr(settings_module, "_OS_CONFIG_COERCED", set())
    config = Settings(_env_file=None, assaf_os_backend="Mysql")
    assert config.assaf_os_backend == "sheets"
    assert get_os_backend(assaf.settings(), OWNER, actor="OS").kind == "sheets"
    # Even an uncoerced garbage value is never read as postgres: only the exact word is.
    config = assaf.settings()
    config.assaf_os_backend = "POSTGRES"
    assert get_os_backend(config, OWNER, actor="OS").kind == "sheets"


def test_a_sheets_construction_refusal_is_logged_and_raised(caplog):
    config = assaf.settings()
    config.assaf_os_enabled = False
    with caplog.at_level(logging.INFO, logger=LOGGER):
        with pytest.raises(port.AssafOsError) as refused:
            get_os_backend(config, OWNER, actor="OS")
    assert (refused.value.code, refused.value.status) == ("operation_unavailable", 404)
    assert backend_lines(caplog) == [
        "os_backend op=select outcome=refused reason=operation_unavailable"
    ]


# --------------------------------------------------------------------------- Sheets wrapper


class RecordingPort:
    owner_id = OWNER
    port_only = "kept"

    def __init__(self):
        self.calls = []

    def _record(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        return {"via": name}

    def schema(self, *args, **kwargs):
        return self._record("schema", *args, **kwargs)

    def snapshot(self, *args, **kwargs):
        return self._record("snapshot", *args, **kwargs)

    def propose(self, *args, **kwargs):
        return self._record("propose", *args, **kwargs)

    def proposal(self, *args, **kwargs):
        return self._record("proposal", *args, **kwargs)

    def decide(self, *args, **kwargs):
        return self._record("decide", *args, **kwargs)

    def receipt(self, *args, **kwargs):
        return self._record("receipt", *args, **kwargs)

    def port_only_method(self, value):
        return ("port", value)


def test_the_wrapper_passes_every_argument_through_unchanged():
    inner = RecordingPort()
    backend = SheetsOsBackend(inner)
    request = object()
    assert backend.schema() == {"via": "schema"}
    assert backend.propose(request) == {"via": "propose"}
    assert backend.proposal("PR_1", budget=10.0) == {"via": "proposal"}
    assert backend.decide("approve", "PR_1", request) == {"via": "decide"}
    assert backend.receipt("req-1") == {"via": "receipt"}
    assert inner.calls == [
        ("schema", (), {}),
        ("propose", (request,), {}),
        ("proposal", ("PR_1",), {"budget": 10.0}),
        ("decide", ("approve", "PR_1", request), {}),
        ("receipt", ("req-1",), {}),
    ]


def test_the_wrapper_snapshot_ignores_the_query():
    inner = RecordingPort()
    backend = SheetsOsBackend(inner)
    assert backend.snapshot() == backend.snapshot(SnapshotQuery(entity="leads", limit=3))
    assert inner.calls == [("snapshot", (), {}), ("snapshot", (), {})]


def test_the_wrapper_has_no_proposal_request_operation():
    with pytest.raises(port.AssafOsError) as refused:
        SheetsOsBackend(RecordingPort()).proposal_request("req-1")
    assert (refused.value.code, refused.value.status) == ("operation_unavailable", 404)


def test_other_attributes_are_the_ports_and_guards_do_not_recurse():
    backend = SheetsOsBackend(RecordingPort())
    assert backend.kind == "sheets"
    assert backend.port_only == "kept" and backend.owner_id == OWNER
    assert backend.port_only_method(3) == ("port", 3)
    assert backend._record("x") == {"via": "x"}, "legacy lines may reach any port member"
    with pytest.raises(AttributeError):
        backend.not_on_the_port
    bare = SheetsOsBackend.__new__(SheetsOsBackend)  # as a copy or a pickle probe builds it
    with pytest.raises(AttributeError):
        bare.anything
    with pytest.raises(AttributeError):
        bare.__deepcopy__


# --------------------------------------------------------------------------- the real routes


def transport(config, calls):
    def respond(_url, raw, _budget):
        request = json.loads(json.loads(raw)["payload"])
        op = request["operation"]
        calls.append(op)
        if op == "schema":
            result = assaf._schema(config)
        elif op == "snapshot":
            result = assaf._snapshot(config)
        elif op in {"propose", "proposal"}:
            result = assaf._pending(config, "OS")
        elif op == "receipt":
            result = {
                **{k: v for k, v in assaf._schema(config).items() if k in {
                    "api_version", "source", "mode", "workbook_id", "workspace_id"
                }},
                "actor_source": "OS", "status": "unknown",
                "request_id": request["data"]["request_id"], "proposal_id": None,
                "entity": None, "record_id": None, "revision": None, "committed_at": None,
                "reason_codes": ["receipt_not_found"],
            }
        else:
            result = assaf._decision_result(
                config, request, status="rejected" if op == "reject" else "committed"
            )
        return assaf.signed(raw, result, config)

    return respond


def ledger_client(sessions_factory, config):
    app = auth._app(sessions_factory, config)
    app.include_router(api.router)
    app.add_middleware(OwnerOSBoundary)
    app.add_exception_handler(HTTPException, owner_http_error)
    app.add_exception_handler(RequestValidationError, owner_validation_error)
    app.add_exception_handler(LedgerError, ledger_error_handler)
    return TestClient(app, base_url=auth.ORIGIN)


@pytest.fixture
def setup(sessions, monkeypatch):
    config = assaf.settings()
    calls = []
    monkeypatch.setattr(port, "_post", transport(config, calls))
    with ledger_client(sessions, config) as browser:
        login = auth._login(browser)
        headers = {"Origin": auth.ORIGIN, "X-CSRF-Token": login["csrf_token"]}
        yield config, browser, headers, calls


@pytest.fixture
def authority(monkeypatch):
    """Drives ``core_authority_state`` (the fence reads nothing else) and records each call."""
    seen = []
    box = {"result": None}

    def fake(settings):
        seen.append(settings)
        result = box["result"]
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(ledger_api, "core_authority_state", fake)
    return SimpleNamespace(seen=seen, set=lambda value: box.update(result=value))


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


def warm(config):
    port._remember_decision_binding(config, OWNER, assaf._pending(config, "OS"))


def write(browser, headers, config, op):
    return browser.post(path(op), headers=headers, json=body(op, config))


def assert_refused(response, status, code, *, op, caplog):
    """The v1 envelope with write_outcome not_started, and the single owner_os_write line."""
    assert response.status_code == status, response.text
    assert set(response.json()) == {"api_version", "request_id", "error"}
    assert response.json()["error"] == {
        "code": code, "retryable": status in {429, 503, 504}, "write_outcome": "not_started",
    }
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("owner_os_write ")]
    assert lines == [f"owner_os_write op={op} outcome=not_started reason={code}"]
    assert SENTINEL not in caplog.text


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("state", ("active", "restoring"))
def test_a_sheets_write_is_refused_while_core_is_active_or_restoring(
    setup, authority, op, state, caplog
):
    config, browser, headers, calls = setup
    authority.set(state)
    warm(config)
    with caplog.at_level(logging.INFO):
        response = write(browser, headers, config, op)
    assert calls == [], "nothing reached the Apps Script"
    assert authority.seen == [config], "exactly one authority read, through core_authority_state"
    assert_refused(response, 409, "authority_moved", op=op, caplog=caplog)


@pytest.mark.parametrize("op", ("approve", "reject"))
def test_a_cold_decision_is_refused_after_its_read_and_still_not_started(
    setup, authority, op, caplog
):
    config, browser, headers, calls = setup
    authority.set("active")
    with caplog.at_level(logging.INFO):
        response = write(browser, headers, config, op)
    assert calls == ["proposal"], "only the read ran; the write was never dispatched"
    assert authority.seen == [config]
    assert_refused(response, 409, "authority_moved", op=op, caplog=caplog)


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("state", ("shadow", "fenced", None))
def test_a_sheets_write_proceeds_on_shadow_fenced_and_no_state(
    setup, authority, op, state, caplog
):
    config, browser, headers, calls = setup
    authority.set(state)
    warm(config)
    with caplog.at_level(logging.INFO):
        response = write(browser, headers, config, op)
    assert response.status_code == 200, response.text
    assert calls == [op] and authority.seen == [config]
    assert "write_outcome" not in response.text
    assert not [r for r in caplog.records if r.getMessage().startswith("owner_os_write ")]


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize(
    "failure",
    (
        LedgerError("ledger_unavailable", reason="read_failed"),
        RuntimeError(SENTINEL),
        "unexpected_state",
    ),
    ids=("ledger_error", "leaked_exception", "unknown_state"),
)
def test_an_unreadable_authority_state_is_503_and_not_started(
    setup, authority, op, failure, caplog
):
    config, browser, headers, calls = setup
    authority.set(failure)
    warm(config)
    with caplog.at_level(logging.INFO):
        response = write(browser, headers, config, op)
    assert calls == []
    assert_refused(response, 503, "ledger_unavailable", op=op, caplog=caplog)


def loader(result, loaded):
    """A stand-in for ``api._implementation`` that serves the V2-16 ``core_state`` reader."""

    def load(module, name):
        loaded.append((module, name))
        if isinstance(result, LedgerError):  # the V2-16 module is not built yet
            raise result

        def core_state(settings):
            if isinstance(result, BaseException):  # a database error
                raise result
            return result

        return core_state

    return load


@pytest.mark.parametrize("op", OPS)
def test_with_no_dsn_the_real_core_authority_state_proceeds_without_loading_anything(
    setup, monkeypatch, op
):
    config, browser, headers, calls = setup
    loaded = []
    monkeypatch.setattr(ledger_api, "_implementation", loader("never", loaded))
    warm(config)
    assert write(browser, headers, config, op).status_code == 200
    assert loaded == [] and calls == [op]


@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize(
    "result,status,code",
    [
        (None, 200, None),  # a DSN but no os_workspaces row: the Sheets write proceeds
        ("shadow", 200, None),
        ("fenced", 200, None),
        ("active", 409, "authority_moved"),
        ("restoring", 409, "authority_moved"),
        (RuntimeError(SENTINEL), 503, "ledger_unavailable"),  # the database is down
        (LedgerError("ledger_unavailable", reason="implementation_missing"), 503,
         "ledger_unavailable"),
    ],
    ids=("no_row", "shadow", "fenced", "active", "restoring", "db_error", "module_missing"),
)
def test_with_a_dsn_the_real_core_authority_state_drives_the_fence(
    setup, monkeypatch, op, result, status, code, caplog
):
    config, browser, headers, calls = setup
    real, loaded = ledger_api.core_authority_state, []
    dsn = SimpleNamespace(owner_os_ledger_database_url="postgresql://ledger.invalid/os")
    monkeypatch.setattr(ledger_api, "core_authority_state", lambda settings: real(dsn))
    monkeypatch.setattr(ledger_api, "_implementation", loader(result, loaded))
    warm(config)
    with caplog.at_level(logging.INFO):
        response = write(browser, headers, config, op)
    assert loaded == [("app.services.os_ledger.authority", "core_state")]
    assert response.status_code == status, response.text
    if code is None:
        assert calls == [op]
    else:
        assert calls == []
        assert_refused(response, status, code, op=op, caplog=caplog)
    assert SENTINEL not in caplog.text


def test_freeze_refuses_propose_but_lets_approve_and_reject_through(
    setup, authority, caplog
):
    config, browser, headers, calls = setup
    config.assaf_os_freeze = True
    authority.set("shadow")
    with caplog.at_level(logging.INFO):
        response = write(browser, headers, config, "propose")
    assert calls == [] and authority.seen == [], "freeze is checked before the authority read"
    assert_refused(response, 503, "os_frozen", op="propose", caplog=caplog)
    assert response.json()["error"]["retryable"] is True
    for op in ("approve", "reject"):
        warm(config)
        assert write(browser, headers, config, op).status_code == 200
    assert calls == ["approve", "reject"]


@pytest.mark.parametrize("op", ("approve", "reject"))
def test_freeze_never_overrides_the_authority_refusal_of_a_decision(setup, authority, op, caplog):
    config, browser, headers, calls = setup
    config.assaf_os_freeze = True
    authority.set("active")
    warm(config)
    with caplog.at_level(logging.INFO):
        response = write(browser, headers, config, op)
    assert calls == []
    assert_refused(response, 409, "authority_moved", op=op, caplog=caplog)


def pending_request(config, source="MIA"):
    pending = assaf._pending(config, source)
    return port.ProposeRequest(**{key: pending[key] for key in (
        "request_id", "entity", "record_id", "expected_revision", "input")})


def attempt(adapter, config, op):
    """One write through the public port method for ``op`` (the OS binding is warmed first)."""
    warm(config)
    if op == "propose":
        return adapter.propose(pending_request(config, adapter.actor_source))
    key = "approval_request_id" if op == "approve" else "rejection_request_id"
    request = port.DecisionRequest(request_id=assaf._pending(config)[key])
    return adapter.decide(op, assaf.PROPOSAL, request)


@pytest.mark.parametrize("op", OPS)
def test_the_kill_switch_still_precedes_the_fence(monkeypatch, authority, op):
    config = assaf.settings()
    sent = []
    monkeypatch.setattr(port, "_post", transport(config, sent))
    authority.set("active")
    config.kill_switch = True
    source = "MIA" if op == "propose" else "OS"
    adapter = port.AssafOsPort(config, OWNER, actor_source=source)
    with pytest.raises(port.AssafOsError) as refused:
        attempt(adapter, config, op)
    assert (refused.value.code, refused.value.status) == ("kill_switch", 403)
    assert sent == [] and authority.seen == []


def test_reads_never_consult_the_authority_state(setup, authority):
    config, browser, headers, calls = setup
    authority.set(LedgerError("ledger_unavailable"))
    config.assaf_os_freeze = True
    paths = (
        ROOT + "/schema",
        ROOT,
        f"{ROOT}/proposals/{assaf.PROPOSAL}",
        f"{ROOT}/receipts/{assaf.REQUEST}",
    )
    for url in paths:
        assert browser.get(url, headers=headers).status_code == 200, url
    assert calls == ["schema", "snapshot", "proposal", "receipt"]
    assert authority.seen == []


@pytest.mark.parametrize("source,op", [
    ("MIA", "propose"),
    ("OS", "propose"),
    ("OS", "approve"),
    ("OS", "reject"),
    ("TELEGRAM_APPROVAL", "approve"),
    ("TELEGRAM_APPROVAL", "reject"),
])
def test_the_fence_holds_for_every_actor_outside_http(monkeypatch, authority, source, op):
    config = assaf.settings()
    sent = []
    monkeypatch.setattr(port, "_post", transport(config, sent))
    adapter = port.AssafOsPort(config, OWNER, actor_source=source)
    authority.set("restoring")
    with pytest.raises(port.AssafOsError) as refused:
        attempt(adapter, config, op)
    assert (refused.value.code, refused.value.status) == ("authority_moved", 409)
    assert sent == []
    authority.set("fenced")
    assert attempt(adapter, config, op)["status"] in {"pending", "committed", "rejected"}
    assert sent[-1] == op


# --------------------------------------------------------------------------- construction fence


def test_constructing_the_port_under_postgres_is_authority_moved():
    with pytest.raises(port.AssafOsError) as refused:
        port.AssafOsPort(pg_settings(), OWNER, actor_source="OS")
    assert (refused.value.code, refused.value.status) == ("authority_moved", 409)


def test_the_construction_fence_is_the_first_statement():
    broken = pg_settings(assaf_os_enabled=False, assaf_os_script_url="https://wrong.invalid")
    for owner in (OWNER, "99999999", "not-numeric"):
        with pytest.raises(port.AssafOsError) as refused:
            port.AssafOsPort(broken, owner, actor_source="MIA")
        assert (refused.value.code, refused.value.status) == ("authority_moved", 409)
    # Sheets keeps every pre-existing refusal exactly as it was.
    broken.assaf_os_backend = "sheets"
    with pytest.raises(port.AssafOsError) as refused:
        port.AssafOsPort(broken, OWNER, actor_source="MIA")
    assert (refused.value.code, refused.value.status) == ("operation_unavailable", 404)


def test_the_snapshot_cache_proof_fails_closed_under_postgres():
    with pytest.raises(cache._Refusal) as refused:
        cache._prove_configuration(pg_settings(), OWNER)
    assert refused.value.reason == "binding_mismatch"
    cache._prove_configuration(assaf.settings(), OWNER)


# --------------------------------------------------------------------------- snapshot query


def test_the_default_query_is_tasks_first_page():
    assert parse_snapshot_query("") == parse_snapshot_query(b"") == SnapshotQuery()
    assert SnapshotQuery() == SnapshotQuery("tasks", None, 50, None)


@pytest.mark.parametrize("query,expected", [
    ("entity=leads", SnapshotQuery(entity="leads")),
    ("entity=clients&limit=1", SnapshotQuery(entity="clients", limit=1)),
    ("limit=200", SnapshotQuery(limit=200)),
    ("after=" + "a" * 512, SnapshotQuery(after="a" * 512)),
    ("since=" + "9" * 64, SnapshotQuery(since="9" * 64)),
    ("since=12.40.3", SnapshotQuery(since="12.40.3")),
    ("entity=time&after=eyJ2IjoxfQ.sig-_&limit=75",
     SnapshotQuery(entity="time", after="eyJ2IjoxfQ.sig-_", limit=75)),
    ("after=a%2Bb%3D", SnapshotQuery(after="a+b=")),
] + [(f"entity={e}", SnapshotQuery(entity=e)) for e in (
    "leads", "clients", "works", "payments", "time", "expenses", "tasks")])
def test_valid_queries_parse_into_the_frozen_dataclass(query, expected):
    assert parse_snapshot_query(query) == expected
    with pytest.raises(AttributeError):
        expected.limit = 1


INVALID_QUERIES = {
    "unknown key": "foo=1",
    "unknown key beside valid": "entity=leads&page=2",
    "key case": "Entity=leads",
    "duplicate key": "entity=leads&entity=tasks",
    "duplicate equal key": "limit=5&limit=5",
    "duplicate blank": "after=x&after=",
    "entity not listed": "entity=nope",
    "entity promote_lead": "entity=promote_lead",
    "entity case": "entity=Leads",
    "entity empty": "entity=",
    "limit zero": "limit=0",
    "limit over": "limit=201",
    "limit far over": "limit=1000",
    "limit negative": "limit=-1",
    "limit text": "limit=abc",
    "limit empty": "limit=",
    "limit leading zero": "limit=05",
    "limit plus": "limit=%2B5",
    "limit decimal": "limit=1.5",
    "limit exponent": "limit=1e2",
    "limit space": "limit=%205",
    "limit non-ascii digit": "limit=%D9%A3",
    "after too long": "after=" + "a" * 513,
    "since too long": "since=" + "9" * 65,
    "after empty": "after=",
    "since empty": "since=",
    "after with since": "after=abc&since=1.2.3",
    "after with blank since": "since=&after=abc",
    "bare key": "entity",
    "bare ampersand": "&",
    "trailing ampersand": "entity=leads&",
    "leading ampersand": "&entity=leads",
    "empty field": "entity=leads&&limit=5",
    "bad utf-8": "after=%ff",
    "nul": "after=%00",
    "space": "after=a%20b",
    "tab": "since=1%092",
    "newline": "after=a%0Ab",
    "non-ascii": "after=%C3%A9",
    "too many fields": "&".join(f"k{i}=1" for i in range(40)),
}


@pytest.mark.parametrize("query", INVALID_QUERIES.values(), ids=INVALID_QUERIES.keys())
def test_every_refused_query_is_invalid_query(query):
    with pytest.raises(LedgerError) as refused:
        parse_snapshot_query(query)
    assert (refused.value.code, refused.value.status) == ("invalid_query", 400)
    assert refused.value.retryable is False


def test_a_non_ascii_raw_query_is_invalid():
    with pytest.raises(LedgerError) as refused:
        parse_snapshot_query("after=é".encode())
    assert refused.value.code == "invalid_query"


@pytest.mark.parametrize("kwargs", [
    {"entity": "nope"}, {"entity": 1}, {"limit": 0}, {"limit": 201}, {"limit": True},
    {"limit": "5"}, {"after": ""}, {"after": "x" * 513}, {"since": "x" * 65},
    {"after": "a", "since": "b"}, {"after": 5},
])
def test_the_dataclass_refuses_what_the_parser_refuses(kwargs):
    with pytest.raises(LedgerError) as refused:
        SnapshotQuery(**kwargs)
    assert refused.value.code == "invalid_query"


@pytest.fixture
def pg_setup(sessions, pg_stub):
    config = pg_settings()
    with ledger_client(sessions, config) as browser:
        login = auth._login(browser)
        headers = {"Origin": auth.ORIGIN, "X-CSRF-Token": login["csrf_token"]}
        yield config, browser, headers, pg_stub


def test_postgres_route_default_query_and_stub_construction(pg_setup):
    config, browser, headers, stub = pg_setup
    response = browser.get(ROOT, headers=headers)
    assert response.status_code == 200 and response.json() == {
        "stub": "snapshot", "entity": "tasks"
    }
    assert stub.queries == [SnapshotQuery()]
    built = stub.instances[0].args
    assert built[0] is config and built[1] == OWNER and built[2] == "OS"
    assert built[3].owner_id == OWNER, "the session principal is handed to the backend"


def test_postgres_route_passes_the_parsed_query(pg_setup):
    _, browser, headers, stub = pg_setup
    response = browser.get(
        ROOT + "?entity=payments&limit=200&since=3.4.5", headers=headers
    )
    assert response.status_code == 200 and response.json()["entity"] == "payments"
    assert stub.queries == [SnapshotQuery(entity="payments", limit=200, since="3.4.5")]


def test_postgres_route_refuses_every_bad_query_with_the_v2_envelope(pg_setup, caplog):
    _, browser, headers, stub = pg_setup
    with caplog.at_level(logging.INFO):
        for name, query in INVALID_QUERIES.items():
            response = browser.get(f"{ROOT}?{query}", headers=headers)
            assert response.status_code == 400, (name, response.text)
            envelope = response.json()
            assert set(envelope) == {"api_version", "request_id", "error"}, name
            assert envelope["api_version"] == "owner-os.v1"
            assert envelope["error"] == {"code": "invalid_query", "retryable": False}, name
    assert stub.queries == [], "an invalid query never reaches the backend"
    assert "invalid_query" in caplog.text


def test_a_ledger_error_from_the_backend_reaches_the_v2_handler_unchanged(pg_setup):
    config, browser, headers, stub = pg_setup
    stub.failures = {
        "propose": LedgerError("os_frozen"),
        "decide": LedgerError("authority_changed"),
        "snapshot": LedgerError("cursor_invalidated", details={"observed_version": 9}),
    }
    frozen = write(browser, headers, config, "propose")
    assert frozen.status_code == 503
    assert frozen.json()["error"] == {
        "code": "os_frozen", "retryable": True, "write_outcome": "not_started",
    }
    changed = write(browser, headers, config, "approve")
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "authority_changed"
    stale = browser.get(ROOT, headers=headers)
    assert stale.status_code == 409
    assert stale.json()["error"] == {
        "code": "cursor_invalidated", "retryable": False, "details": {"observed_version": 9},
    }


@pytest.mark.parametrize("ready", ("ledger_off", "epoch_zero", "module_missing"))
def test_postgres_routes_are_503_when_the_ledger_is_not_ready(sessions, monkeypatch, ready):
    config = pg_settings()
    if ready == "ledger_off":
        config.assaf_os_ledger_enabled = False
    elif ready == "epoch_zero":
        config.assaf_os_authority_epoch = 0
    monkeypatch.setitem(sys.modules, PG_MODULE, None)
    calls = []
    monkeypatch.setattr(port, "_post", transport(config, calls))
    with ledger_client(sessions, config) as browser:
        login = auth._login(browser)
        headers = {"Origin": auth.ORIGIN, "X-CSRF-Token": login["csrf_token"]}
        read = browser.get(ROOT, headers=headers)
        wrote = write(browser, headers, config, "propose")
    assert calls == [], "postgres never falls back to the Apps Script"
    assert read.status_code == 503 and read.json()["error"] == {
        "code": "ledger_unavailable", "retryable": True,
    }
    assert wrote.status_code == 503 and wrote.json()["error"] == {
        "code": "ledger_unavailable", "retryable": True, "write_outcome": "not_started",
    }


# --------------------------------------------------------------------------- v1 golden


def test_the_v1_snapshot_body_is_byte_identical_through_the_factory(setup, monkeypatch):
    config, browser, headers, calls = setup
    fixed = assaf._snapshot(config)
    calls.clear()

    def respond(_url, raw, _budget):
        calls.append(json.loads(json.loads(raw)["payload"])["operation"])
        return assaf.signed(raw, fixed, config)

    monkeypatch.setattr(port, "_post", respond)
    refreshed = []
    monkeypatch.setattr(api, "remember_snapshot", lambda *args: refreshed.append(args[1]))

    through_factory = browser.get(ROOT, headers=headers)
    # The query allow-list belongs to postgres alone: under sheets every query is ignored.
    ignored = browser.get(
        ROOT + "?entity=nope&bogus=1&bogus=2&limit=0&after=x&since=y", headers=headers
    )
    # The pre-V2-17 body: the Apps Script port's own result, rendered by the route as before.
    legacy = port.AssafOsPort(config, OWNER, actor_source="OS").snapshot()
    golden = JSONResponse(jsonable_encoder(legacy)).body

    assert through_factory.status_code == ignored.status_code == 200
    assert through_factory.content == ignored.content == golden
    assert through_factory.json() == fixed
    assert {k: v for k, v in through_factory.headers.items() if k != "server-timing"} == {
        k: v for k, v in ignored.headers.items() if k != "server-timing"
    }
    assert calls == ["snapshot"] * 3
    assert refreshed == [OWNER, OWNER], "the instant-open cache is still fed under sheets"


def test_the_cache_refresh_runs_for_sheets_only(setup, monkeypatch, pg_stub):
    config, browser, headers, calls = setup
    refreshed = []
    monkeypatch.setattr(api, "remember_snapshot", lambda *args: refreshed.append(args[1]))
    assert browser.get(ROOT, headers=headers).status_code == 200
    assert refreshed == [OWNER]
    refreshed.clear()
    config.assaf_os_backend = "postgres"
    config.assaf_os_ledger_enabled = True
    config.assaf_os_authority_epoch = 1
    response = browser.get(ROOT, headers=headers)
    assert response.status_code == 200 and response.json()["stub"] == "snapshot"
    assert refreshed == [], "a postgres snapshot is never written into the Sheets cache"
    assert calls == ["snapshot"], "only the sheets read reached the Apps Script"


# --------------------------------------------------------------------------- the grep test


def _parents(tree):
    result = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            result[child] = node
    return result


def port_references(source: str):
    """Every way app code could build or smuggle the port: (kind, line, enclosing function)."""
    tree = ast.parse(source)
    parents = _parents(tree)

    def enclosing(node):
        while node in parents:
            node = parents[node]
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return node.name
        return None

    def in_annotation(node):
        child = node
        while child in parents:
            parent = parents[child]
            if isinstance(parent, ast.arg) and parent.annotation is child:
                return True
            if isinstance(parent, ast.AnnAssign) and parent.annotation is child:
                return True
            functions = (ast.FunctionDef, ast.AsyncFunctionDef)
            if isinstance(parent, functions) and parent.returns is child:
                return True
            child = parent
        return False

    found = []
    calls = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            callee = node.func
            name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
            if name == "AssafOsPort":
                found.append(("call", node.lineno, enclosing(node)))
                calls.add(callee)
    for node in ast.walk(tree):
        # Any other load of the name (an alias, functools.partial, __new__, a dict of factories)
        # could smuggle the port past the factory; type annotations are not loads at run time.
        loaded = (isinstance(node, ast.Name) and node.id == "AssafOsPort") or (
            isinstance(node, ast.Attribute) and node.attr == "AssafOsPort"
        )
        if loaded and isinstance(node.ctx, ast.Load) and node not in calls:
            if not in_annotation(node):
                found.append(("load", node.lineno, enclosing(node)))
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name == "AssafOsPort" and alias.asname:
                    found.append(("alias", node.lineno, enclosing(node)))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value == "AssafOsPort" or node.value.endswith(".AssafOsPort"):
                found.append(("string", node.lineno, enclosing(node)))
    return found


def app_sources():
    for file in sorted(APP_ROOT.rglob("*.py")):
        if "__pycache__" not in file.parts:
            yield file.relative_to(APP_ROOT.parent).as_posix(), file.read_text(encoding="utf-8")


ALLOWED_CONSTRUCTION = {
    ("app/services/os_backend.py", "get_os_backend"),
    ("app/services/assaf_os_snapshot_cache.py", "_prove_configuration"),
}


def test_only_the_factory_and_the_snapshot_cache_proof_construct_the_port():
    constructions, smuggled = set(), []
    for relative, source in app_sources():
        for kind, line, function in port_references(source):
            if kind == "call":
                constructions.add((relative, function))
            else:
                smuggled.append((relative, line, kind))
    assert constructions == ALLOWED_CONSTRUCTION
    assert smuggled == []


def test_the_scanner_catches_every_construction_form():
    forms = {
        "direct": "AssafOsPort(s, o, actor_source='OS')",
        "attribute": "import app.services.assaf_os as m\nm.AssafOsPort(s, o)",
        "nested": "def f():\n    def g():\n        return AssafOsPort(s)\n",
        "chained": "AssafOsPort(s, o, actor_source='OS').snapshot()",
        "alias": "from app.services.assaf_os import AssafOsPort as Port",
        "getattr": "getattr(m, 'AssafOsPort')",
        "dotted": "monkeypatch.setattr('app.services.assaf_os.AssafOsPort', X)",
    }
    for name, source in forms.items():
        assert port_references(source), name
    assert port_references("from app.services.assaf_os import AssafOsError\nx = 1") == []
    assert port_references("'AssafOsPort.snapshot() returned'") == []
    assert port_references("def f() -> AssafOsPort: ...") == []


CALL_SITES = {
    "app/api/assaf_os.py": 1,
    "app/tools/owner/assaf_os.py": 1,
    "app/services/assaf_os_leads.py": 2,
    "app/services/owner_actions.py": 4,
    # V2-31: the read-only Sheets probe of the cutover CLI (pending proposals count, actor MIA).
    "app/workers/os_authority.py": 1,
}


def test_every_constructor_site_calls_the_factory():
    seen = {}
    for relative, source in app_sources():
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", getattr(node.func, "attr", None))
                if name == "get_os_backend" and relative != "app/services/os_backend.py":
                    keywords = {k.arg: k.value for k in node.keywords}
                    assert isinstance(keywords["actor"], ast.Constant), (relative, node.lineno)
                    assert keywords["actor"].value in {"OS", "MIA", "TELEGRAM_APPROVAL"}
                    assert "actor_source" not in keywords
                    seen[relative] = seen.get(relative, 0) + 1
    assert seen == CALL_SITES and sum(seen.values()) == 9
