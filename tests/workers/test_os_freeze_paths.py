"""V2-31: the OS freeze (the T1 drain) holds on every write path (contract I8, sections 7, 16).

``assaf_os_freeze`` is the first cutover step. It must stop every NEW intent while the pending ones
still settle (decide and reject stay allowed), because ``activate`` refuses unless the drain left
zero pending proposals, zero unknown receipts and zero open cards. The proof has three layers.

1. A census of the source. Every call to ``.propose(`` or ``.decide(`` in ``app/`` is either a
   classified Assaf OS write site or a classified look-alike (the Mia CRM quick leads, the
   prototype oracle, the backend wrapper). A new site, or a moved one, fails here until someone
   classifies it. The same goes for everything that can reach the Script transport, every
   Sheets-API writer and every operation the port can send.
2. Every classified site is DRIVEN through its real entry point (HTTP route, owner tool, lead
   preparation, Telegram approval) with the freeze on, against the real ``AssafOsPort`` and a
   recording transport: an intent is refused ``os_frozen`` with nothing sent, a decision still
   reaches the Script. The same entry point with the freeze off reaches the transport, so a
   refusal cannot come from a test that never got near the code.
3. The Script itself refuses under ``ASSAF_OS_MODE=FENCED``
   (tests/assaf-os/apps-script-fence.test.js).
"""

from __future__ import annotations

import ast
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest
from app.api import assaf_os as api
from app.api.owner_os_boundary import OwnerOSBoundary, owner_http_error, owner_validation_error
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.base import Base
from app.db.session import make_engine
from app.db.store import LeadStore
from app.domain.approvals import DECISION_APPROVED, DECISION_REJECTED
from app.integrations.sheets import validate_owner_sheet_request
from app.services import assaf_os as port
from app.services.assaf_os_leads import prepare_selected_crm_lead
from app.services.os_ledger import api as ledger_api
from app.services.owner_actions import (
    decide_owner_action,
    execute_approved_owner_action_with_adapters,
    reject_assaf_os_owner_action,
)
from app.tools.owner.assaf_os import _assaf_os_propose
from app.tools.owner.types import ToolContext
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker
from starlette.exceptions import HTTPException

from tests.unit import test_assaf_os as assaf
from tests.unit import test_owner_sessions as auth
from tests.unit import test_website_lead_approval as leads

sessions = auth.sessions
ROOT_DIR = Path(__file__).resolve().parents[2]
ROUTE = "/v1/owner/os/unified"
OWNER = leads.OWNER

# --------------------------------------------------------------------------------- the census

# A NEW Assaf OS intent: refused during the drain.
OS_INTENT_SITES = {
    ("app/api/assaf_os.py", "propose", "propose"): "POST /proposals (the OS screen)",
    ("app/tools/owner/assaf_os.py", "_assaf_os_propose", "propose"): "Mia owner tool",
    ("app/services/assaf_os_leads.py", "_create_final_proposal", "propose"): "website lead card",
}
# Settles an EXISTING proposal: allowed during the drain, so that it can empty.
OS_DECISION_SITES = {
    ("app/api/assaf_os.py", "approve", "decide"): "POST /proposals/{id}/approve",
    ("app/api/assaf_os.py", "reject", "decide"): "POST /proposals/{id}/reject",
    ("app/services/owner_actions.py", "run", "decide"): "Telegram approve",
    ("app/services/owner_actions.py", "reject_assaf_os_owner_action", "decide"): "Telegram reject",
}
# The same method names with no Apps Script behind them.
LOOK_ALIKES = {
    ("app/api/owner_os_actions.py", "quick_lead_proposal", "propose"): "Mia CRM quick lead",
    ("app/api/quick_leads.py", "propose_quick_lead", "propose"): "Mia CRM quick lead",
    ("app/prototypes/assaf_os_adapter.py", "propose", "propose"): "V2-13 prototype oracle",
    ("app/prototypes/assaf_os_adapter.py", "decide", "decide"): "V2-13 prototype oracle",
    ("app/services/os_backend.py", "propose", "propose"): "SheetsOsBackend, delegates to the port",
    ("app/services/os_backend.py", "decide", "decide"): "SheetsOsBackend, delegates to the port",
}
# Files that may talk to the Script transport, and why.
TRANSPORT_USERS = {
    "app/services/assaf_os.py": "the port: the only sender (AssafOsPort._call -> _post)",
    "app/workers/os_authority.py": "mia os-authority: one read-only signed `schema` probe",
}
# Files that mention the Script URL or the transport without sending (configuration, cache key).
TRANSPORT_MENTIONS = {
    "app/core/config.py": "the Settings field",
    "app/services/assaf_os_snapshot_cache.py": "a keyed digest of the binding, no network",
}
# A different Apps Script (the business ledger) that talks to its own workbook; it has its own
# transport and is policy-denied while Assaf OS is enabled (checked below).
OTHER_SCRIPTS = {"app/services/business_ledger.py": "business ledger Script, its own workbook"}
NETWORK_MODULES = {"urllib.request", "http.client", "httpx", "requests", "socket", "aiohttp"}
# The only readers of ``allowed_sheets_spreadsheet_ids()``: each hands it to the Sheets guard.
SHEETS_GUARD_SITES = {
    "app/integrations/sheets.py": 1,
    "app/main.py": 1,
    "app/services/owner_actions.py": 3,
    "app/tools/owner/sheets.py": 3,
}


def app_sources() -> list[tuple[str, str]]:
    return [
        (path.relative_to(ROOT_DIR).as_posix(), path.read_text(encoding="utf-8"))
        for path in sorted((ROOT_DIR / "app").rglob("*.py"))
    ]


def method_sites(
    source: str, methods: frozenset[str], *, with_arguments: bool = False
) -> list[tuple[str, str, int]]:
    """(enclosing function, method, line) for every ``<anything>.<method>(...)`` call.

    ``with_arguments`` keeps only the calls that pass something: ``session.commit()`` never does,
    a ledger ``commit(ctx, request)`` always does.
    """
    found: list[tuple[str, str, int]] = []

    def walk(node: ast.AST, stack: tuple[str, ...]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                walk(child, (*stack, child.name))
                continue
            if (
                isinstance(child, ast.Call)
                and isinstance(child.func, ast.Attribute)
                and child.func.attr in methods
                and (child.args or child.keywords or not with_arguments)
            ):
                found.append((stack[-1] if stack else "<module>", child.func.attr, child.lineno))
            walk(child, stack)

    walk(ast.parse(source), ())
    return found


def imported_modules(source: str) -> set[str]:
    """Every module a file imports, and every ``from x import y`` as ``x`` and ``x.y``."""
    modules: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def opens_connections(source: str) -> set[str]:
    return {
        name for name in imported_modules(source)
        if any(name == net or name.startswith(net + ".") for net in NETWORK_MODULES)
    }


def test_the_census_scanner_sees_every_call_form():
    source = (
        "def a():\n    x.propose(1)\n"
        "async def b():\n    get().decide('a', 'b')\n"
        "def c():\n    def d():\n        return port.propose(2)\n    return d\n"
        "port.propose(3)\n"
    )
    seen = method_sites(source, frozenset({"propose", "decide"}))
    assert [(name, method) for name, method, _ in seen] == [
        ("a", "propose"), ("b", "decide"), ("d", "propose"), ("<module>", "propose"),
    ]
    assert method_sites("def f():\n    return propose(1)\n", frozenset({"propose"})) == []


def test_the_scanner_tells_a_database_commit_from_a_ledger_commit():
    source = (
        "def tx():\n    session.commit()\n    conn.commit()\n"
        "def intent():\n    ledger.commit(ctx, request)\n    ledger.undo(ctx, request_id)\n"
        "    batches.submit(ctx, body)\n    ledger.abandon(ctx, request_id=1)\n"
        "    executor.commit(op='x')\n"
    )
    verbs = frozenset({"commit", "undo", "submit", "abandon"})
    assert [(n, m) for n, m, _ in method_sites(source, verbs, with_arguments=True)] == [
        ("intent", "commit"), ("intent", "undo"), ("intent", "submit"), ("intent", "abandon"),
        ("intent", "commit"),
    ]
    assert len(method_sites(source, verbs)) == 7, "without the filter every commit() counts"


# The ledger-side intents of contract I8 (section 2): commit, undo, batch submit and the assistant
# submit are new intents the T1 freeze must refuse (503 os_frozen); abandon writes no business
# change, so the freeze allows it. None exists in app/ at this base: V2-24 (commit, undo, abandon),
# V2-26 (the statement batch submit) and V2-37 (the assistant submit) land after this chunk, and
# each must add its call site here, classified, in the same change. Keys are
# (file, enclosing function, method) like the sites above; values say why.
LEDGER_INTENT_SITES: dict[tuple[str, str, str], str] = {}
LEDGER_ALLOWED_SITES: dict[tuple[str, str, str], str] = {}
LEDGER_INTENT_VERBS = frozenset({"undo", "submit", "abandon"})


def ledger_intent_sites() -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for relative, source in app_sources():
        for function, method, _ in method_sites(source, LEDGER_INTENT_VERBS):
            found.add((relative, function, method))
        # A database transaction's commit() takes no argument; a ledger commit always does.
        for function, method, _ in method_sites(source, frozenset({"commit"}), with_arguments=True):
            found.add((relative, function, method))
    return found


def test_every_ledger_intent_site_in_app_is_classified():
    found = ledger_intent_sites()
    classified = LEDGER_INTENT_SITES.keys() | LEDGER_ALLOWED_SITES.keys()
    assert found == classified, (
        f"unclassified: {sorted(found - classified)}; stale: {sorted(classified - found)}. "
        "A commit, undo, batch submit or assistant submit is a new intent: the freeze must refuse "
        "it (I8) and a test must drive it with the freeze on, as the OS intents below are."
    )
    assert not (LEDGER_INTENT_SITES.keys() & LEDGER_ALLOWED_SITES.keys())
    assert all(site[2] != "abandon" for site in LEDGER_INTENT_SITES), (
        "abandon writes no business change: the freeze allows it, so it is never an intent site"
    )


def test_every_propose_and_decide_site_in_app_is_classified():
    found = {
        (relative, function, method)
        for relative, source in app_sources()
        for function, method, _ in method_sites(source, frozenset({"propose", "decide"}))
    }
    classified = OS_INTENT_SITES.keys() | OS_DECISION_SITES.keys() | LOOK_ALIKES.keys()
    assert found == classified, (
        f"unclassified: {sorted(found - classified)}; stale: {sorted(classified - found)}"
    )
    assert not (OS_INTENT_SITES.keys() & OS_DECISION_SITES.keys())


def test_the_look_alikes_never_reach_the_script():
    forbidden = ("get_os_backend", "AssafOsPort", "assaf_os_script_url", "os_backend")
    for relative in {site[0] for site in LOOK_ALIKES} - {"app/services/os_backend.py"}:
        text = (ROOT_DIR / relative).read_text(encoding="utf-8")
        assert not [name for name in forbidden if name in text], relative
    quick = (ROOT_DIR / "app/services/quick_leads.py").read_text(encoding="utf-8")
    assert not [name for name in forbidden if name in quick]
    # The prototype oracle is read-only by contract: nothing in app/ imports it for production.
    for relative, source in app_sources():
        if relative.startswith("app/prototypes/"):
            continue
        for node in ast.walk(ast.parse(source)):
            names = []
            if isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
            elif isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            assert not [n for n in names if n.startswith("app.prototypes.assaf_os_adapter")], (
                relative
            )


def test_only_the_port_and_the_authority_probe_can_send_to_the_script():
    senders, mentions = set(), set()
    for relative, source in app_sources():
        tree = ast.parse(source)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant)}
        if "_post" in names | attrs and (
            "assaf_os" in source or relative == "app/services/assaf_os.py"
        ):
            senders.add(relative)
        if "assaf_os_script_url" in attrs | names | strings or "script.google.com" in " ".join(
            s for s in strings if isinstance(s, str)
        ):
            mentions.add(relative)
    assert senders == set(TRANSPORT_USERS), sorted(senders ^ set(TRANSPORT_USERS))
    known = set(TRANSPORT_USERS) | set(TRANSPORT_MENTIONS) | set(OTHER_SCRIPTS)
    assert mentions == known, sorted(mentions ^ known)
    for relative in OTHER_SCRIPTS:
        text = (ROOT_DIR / relative).read_text(encoding="utf-8")
        assert "assaf_os" not in text.lower().replace("business_ledger", ""), relative
    actions = (ROOT_DIR / "app/services/owner_actions.py").read_text(encoding="utf-8")
    denied = (
        'if envelope["kind"].startswith("business."):\n'
        '        if getattr(settings, "assaf_os_enabled", False):\n'
        '            return OwnerActionExecution("policy_denied")'
    )
    assert denied in actions
    # What may only mention the URL must not be able to open a connection at all.
    for relative in TRANSPORT_MENTIONS:
        source = (ROOT_DIR / relative).read_text(encoding="utf-8")
        assert not opens_connections(source), relative


def test_every_sheets_api_writer_reads_the_allow_list_the_freeze_subtracts_from():
    seen: dict[str, int] = {}
    for relative, source in app_sources():
        count = source.count("allowed_sheets_spreadsheet_ids(")
        if count and relative != "app/core/config.py":
            seen[relative] = count
    assert seen == SHEETS_GUARD_SITES
    # A Composio Sheets write can only name an allow-listed workbook (validate_owner_sheet_request).
    book = "OsWorkbook_12345678901234567890"
    live = Settings(_env_file=None, sheets_allowed_spreadsheet_ids=book, assaf_os_workbook_id=book)
    for frozen in (live.model_copy(update={"assaf_os_freeze": True}),
                   live.model_copy(update={"assaf_os_backend": "postgres"})):
        assert book not in frozen.allowed_sheets_spreadsheet_ids()
        with pytest.raises(ValueError, match="not allowlisted"):
            validate_owner_sheet_request(
                spreadsheet_id=book, a1_range="Sheet1!A1:B2", values=[["x", "y"]],
                allowed_spreadsheet_ids=frozen.allowed_sheets_spreadsheet_ids(),
            )
    # Control: before T1 the same write is allowed, so the refusal above is the freeze.
    assert validate_owner_sheet_request(
        spreadsheet_id=book, a1_range="Sheet1!A1:B2", values=[["x", "y"]],
        allowed_spreadsheet_ids=live.allowed_sheets_spreadsheet_ids(),
    )[0] == book


def test_the_operations_the_port_can_send_are_exactly_the_ones_classified():
    assert port.OPERATIONS == {"schema", "snapshot", "proposal", "receipt", "propose", "approve",
                               "reject"}
    assert port.UNCERTAIN_OPERATIONS == {"propose", "approve", "reject"}
    # The fence is placed in _call, on the operation, so a new operation lands in UNCERTAIN or not.
    source = (ROOT_DIR / "app/services/assaf_os.py").read_text(encoding="utf-8")
    assert source.count("if operation in UNCERTAIN_OPERATIONS:\n            # Last refusal") == 1
    assert source.count("self._fence(operation)") == 1


# ------------------------------------------------------------------ the shared drive harness


@pytest.fixture(autouse=True)
def clean_caches():
    port._clear_assaf_os_decision_binding_cache()
    yield
    port._clear_assaf_os_decision_binding_cache()


@dataclass
class Env:
    monkeypatch: pytest.MonkeyPatch
    sessions: object
    tmp_path: Path
    caplog: pytest.LogCaptureFixture


@dataclass
class Outcome:
    refused: str | None  # the refusal code, if the path ended in one
    calls: list[str]  # the operations the transport saw, in order


def transport(config, calls: list[str]):
    """A Script that answers every operation with a validated, signed reply."""
    pending = {}

    def respond(_url, raw, _budget):
        request = json.loads(json.loads(raw)["payload"])
        op = request["operation"]
        calls.append(op)
        if op == "schema":
            result = assaf._schema(config)
        elif op == "snapshot":
            result = assaf._snapshot(config)
        elif op in {"propose", "proposal"}:
            # One reply per actor: a later `proposal` read must equal what `propose` returned.
            actor = request["actor_source"]
            result = pending.setdefault(actor, assaf._pending(config, actor))
        elif op == "receipt":
            result = {
                **{k: v for k, v in assaf._schema(config).items() if k in {
                    "api_version", "source", "mode", "workbook_id", "workspace_id"}},
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


def open_authority(monkeypatch) -> None:
    """Core authority is `shadow` (the pre-cutover state), so only the freeze can refuse."""
    monkeypatch.setattr(ledger_api, "core_authority_state", lambda settings: "shadow")


def new_store(tmp_path: Path):
    engine = make_engine(f"sqlite:///{tmp_path / 'freeze.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(engine)()
    return LeadStore(session), session, engine


def refusal(error: BaseException) -> str:
    return error.code if isinstance(error, port.AssafOsError) else type(error).__name__


# ----- HTTP (the OS screen): propose, approve, reject


def ledger_client(factory, config) -> TestClient:
    app = auth._app(factory, config)
    app.include_router(api.router)
    app.add_middleware(OwnerOSBoundary)
    app.add_exception_handler(HTTPException, owner_http_error)
    app.add_exception_handler(RequestValidationError, owner_validation_error)
    return TestClient(app, base_url=auth.ORIGIN)


def drive_http(env: Env, freeze: bool, op: str) -> Outcome:
    config = assaf.settings()
    config.assaf_os_freeze = freeze
    calls: list[str] = []
    env.monkeypatch.setattr(port, "_post", transport(config, calls))
    open_authority(env.monkeypatch)
    pending = assaf._pending(config, "OS")
    if op == "propose":
        body = {key: pending[key] for key in (
            "request_id", "entity", "record_id", "expected_revision", "input")}
        url = f"{ROUTE}/proposals"
    else:
        key = "approval_request_id" if op == "approve" else "rejection_request_id"
        body, url = {"request_id": pending[key]}, f"{ROUTE}/proposals/{assaf.PROPOSAL}/{op}"
        port._remember_decision_binding(config, auth.OWNER_ID, pending)
    with ledger_client(env.sessions, config) as browser:
        login = auth._login(browser)
        headers = {"Origin": auth.ORIGIN, "X-CSRF-Token": login["csrf_token"]}
        response = browser.post(url, headers=headers, json=body)
    if response.status_code == 200:
        return Outcome(None, calls)
    error = response.json()["error"]
    assert error["write_outcome"] == "not_started", error
    return Outcome(error["code"], calls)


# ----- Mia owner tool, website lead card, Telegram approve and reject


def tool_context(store, config) -> ToolContext:
    return ToolContext(
        store=store, brain=None, settings=config, embedding_port=None,
        principal=Principal.owner(source="telegram", actor_id=OWNER), private_owner_chat=True,
        source_ref="tg:update:freeze-1", owner_text="follow up", now=datetime.now(UTC),
    )


TOOL_ARGUMENTS = {
    "request_id": assaf.REQUEST, "entity": "tasks", "record_id": None, "expected_revision": 0,
    "input_json": json.dumps({"title": "Follow up", "status": "לביצוע"}, ensure_ascii=False),
}


def drive_tool(env: Env, freeze: bool) -> Outcome:
    config = leads._settings(assaf_os_freeze=freeze)
    calls: list[str] = []
    env.monkeypatch.setattr(port, "_post", transport(config, calls))
    open_authority(env.monkeypatch)
    store, session, engine = new_store(env.tmp_path)
    try:
        result = _assaf_os_propose(tool_context(store, config), dict(TOOL_ARGUMENTS))
        return Outcome(None if result.ok else result.error, calls)
    finally:
        session.close()
        engine.dispose()


def drive_leads(env: Env, freeze: bool) -> Outcome:
    config = leads._settings(assaf_os_freeze=freeze)
    calls: list[str] = []
    env.monkeypatch.setattr(port, "_post", transport(config, calls))
    open_authority(env.monkeypatch)
    store, session, engine = new_store(env.tmp_path)
    try:
        leads._seed_source(store)
        try:
            prepare_selected_crm_lead(
                store, settings=config,
                principal=Principal.owner(source="telegram", actor_id=OWNER),
                contact_id="crm-contact-1", conversation_id="site-current", path="לימוד",
                summary="", next_step="",
            )
        except Exception as error:  # the control may fail LATER, on the fake Script's reply
            return Outcome(refusal(error), calls)
        return Outcome(None, calls)
    finally:
        session.close()
        engine.dispose()


def drive_telegram(env: Env, freeze: bool, decision: str) -> Outcome:
    """A card made BEFORE the freeze, decided by the owner, then executed or mirrored AFTER it."""
    config = leads._settings(assaf_os_freeze=False)
    calls: list[str] = []
    env.monkeypatch.setattr(port, "_post", transport(config, calls))
    env.monkeypatch.setattr(
        "app.services.owner_actions._aware", lambda value: value or datetime.now(UTC)
    )
    open_authority(env.monkeypatch)
    store, session, engine = new_store(env.tmp_path)
    actor = Principal.owner(source="telegram", actor_id=OWNER)
    try:
        made = _assaf_os_propose(tool_context(store, config), dict(TOOL_ARGUMENTS))
        assert made.ok, made.error
        decided = decide_owner_action(
            store, principal=actor, approval_id=made.approval_id, decision=decision,
            now=datetime.now(UTC),
        )
        config.assaf_os_freeze = freeze  # T1 starts after the card exists
        calls.clear()
        if decision == DECISION_APPROVED:
            result = execute_approved_owner_action_with_adapters(
                store, settings=config, principal=actor, proposal_id=decided.proposal_id,
            )
            assert result.status == "executed", result.status
        else:
            result = reject_assaf_os_owner_action(
                store, settings=config, principal=actor, proposal_id=decided.proposal_id,
            )
            assert result.status in {"executed", "rejected"}, result.status
        return Outcome(None, calls)
    finally:
        session.close()
        engine.dispose()


INTENT_DRIVERS: dict[tuple[str, str, str], Callable[[Env, bool], Outcome]] = {
    ("app/api/assaf_os.py", "propose", "propose"): lambda env, freeze: drive_http(
        env, freeze, "propose"),
    ("app/tools/owner/assaf_os.py", "_assaf_os_propose", "propose"): drive_tool,
    ("app/services/assaf_os_leads.py", "_create_final_proposal", "propose"): drive_leads,
}
DECISION_DRIVERS: dict[tuple[str, str, str], tuple[str, Callable[[Env, bool], Outcome]]] = {
    ("app/api/assaf_os.py", "approve", "decide"): (
        "approve", lambda env, freeze: drive_http(env, freeze, "approve")),
    ("app/api/assaf_os.py", "reject", "decide"): (
        "reject", lambda env, freeze: drive_http(env, freeze, "reject")),
    ("app/services/owner_actions.py", "run", "decide"): (
        "approve", lambda env, freeze: drive_telegram(env, freeze, DECISION_APPROVED)),
    ("app/services/owner_actions.py", "reject_assaf_os_owner_action", "decide"): (
        "reject", lambda env, freeze: drive_telegram(env, freeze, DECISION_REJECTED)),
}


def test_every_classified_site_has_a_driver_and_nothing_else_does():
    assert set(INTENT_DRIVERS) == set(OS_INTENT_SITES)
    assert set(DECISION_DRIVERS) == set(OS_DECISION_SITES)


@pytest.fixture
def env(monkeypatch, sessions, tmp_path, caplog):
    return Env(monkeypatch, sessions, tmp_path, caplog)


@pytest.mark.parametrize("site", sorted(INTENT_DRIVERS), ids=lambda s: f"{s[0]}::{s[1]}")
def test_an_intent_is_refused_os_frozen_with_nothing_sent_under_the_freeze(env, site):
    outcome = INTENT_DRIVERS[site](env, True)
    assert outcome.refused == "os_frozen", (site, outcome)
    assert "propose" not in outcome.calls, "the Script was never asked"
    # Whatever else the path read before the refusal, it wrote nothing to the Script.
    assert set(outcome.calls) <= {"snapshot", "schema", "proposal", "receipt"}


@pytest.mark.parametrize("site", sorted(INTENT_DRIVERS), ids=lambda s: f"{s[0]}::{s[1]}")
def test_the_same_intent_reaches_the_script_before_the_freeze(env, site):
    outcome = INTENT_DRIVERS[site](env, False)
    assert outcome.refused != "os_frozen", (site, outcome)
    assert "propose" in outcome.calls, (site, outcome)


@pytest.mark.parametrize("site", sorted(DECISION_DRIVERS), ids=lambda s: f"{s[0]}::{s[1]}")
def test_a_decision_still_reaches_the_script_under_the_freeze(env, site):
    operation, driver = DECISION_DRIVERS[site]
    outcome = driver(env, True)
    assert outcome.refused is None, (site, outcome)
    assert operation in outcome.calls, (site, outcome)


def propose_request(config) -> port.ProposeRequest:
    pending = assaf._pending(config)
    return port.ProposeRequest(**{key: pending[key] for key in (
        "request_id", "entity", "record_id", "expected_revision", "input")})


@pytest.mark.parametrize("actor", ("OS", "MIA"))
def test_a_frozen_propose_is_refused_for_every_actor_before_the_authority_is_read(
    monkeypatch, actor
):
    config = leads._settings(assaf_os_freeze=True)
    calls: list[str] = []
    monkeypatch.setattr(port, "_post", transport(config, calls))
    reads: list[object] = []
    monkeypatch.setattr(
        ledger_api, "core_authority_state", lambda settings: reads.append(settings) or "shadow"
    )
    adapter = port.AssafOsPort(config, OWNER, actor_source=actor)
    with pytest.raises(port.AssafOsError) as frozen:
        adapter.propose(propose_request(config))
    assert (frozen.value.code, frozen.value.status) == ("os_frozen", 503)
    assert calls == [] and reads == [], "a frozen propose never reads the authority state"


def test_the_kill_switch_still_outranks_the_freeze(monkeypatch):
    config = leads._settings(assaf_os_freeze=True, kill_switch=True)
    calls: list[str] = []
    monkeypatch.setattr(port, "_post", transport(config, calls))
    open_authority(monkeypatch)
    adapter = port.AssafOsPort(config, OWNER, actor_source="MIA")
    with pytest.raises(port.AssafOsError) as killed:
        adapter.propose(propose_request(config))
    assert (killed.value.code, killed.value.status) == ("kill_switch", 403)
    assert calls == []


def test_a_refused_intent_logs_a_reason_code_and_no_visitor_text(env, caplog):
    caplog.set_level(logging.INFO)
    drive_tool(env, True)
    lines = [r.getMessage() for r in caplog.records if r.name == "mia.os_backend"]
    assert "os_backend op=fence outcome=refused reason=os_frozen operation=propose" in lines
    assert "Follow up" not in caplog.text and "לביצוע" not in caplog.text
