"""V2-28: website leads on the ledger (V2_PG_CONTRACTS.md section 13, "Website leads").

The final lead proposal is a MIA ledger proposal whose id is
``mia_request_uuid(owner, ws, "website-lead-<hex>")``; the duplicate pre-check is a ledger read; a
duplicate is ``lead_already_exists``, a definite no-write with no ``pending_review`` row; and no
Mia path can reach decide or commit. FakeLedger only.
"""

from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path

import pytest
from app.capabilities.types import Principal
from app.db.models import ApprovalRow
from app.db.store import LeadStore
from app.services import assaf_os_leads
from app.services.assaf_os import AssafOsError
from app.services.assaf_os_leads import (
    FINAL_KIND,
    choose_lead_path,
    prepare_lead_choice,
    prepare_selected_crm_lead,
    recover_expired_approved_crm_lead,
)
from app.services.os_ledger import api
from app.services.owner_actions import (
    execute_approved_owner_action_with_adapters,
    is_ledger_owner_action,
    read_owner_action,
)
from app.workers.crm_runtime import legacy_sheet_projection_enabled

from tests.services.os_v2_28_support import (
    OWNER,
    WORKSPACE,
    LedgerWorld,
    approve_row,
    make_store_factory,
    pg_settings,
    provider_claims,
    seed_lead_source,
)

PRINCIPAL = Principal.owner(source="telegram", actor_id=OWNER)
REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def store(tmp_path):
    session = make_store_factory(tmp_path)()
    yield LeadStore(session)
    session.close()


@pytest.fixture
def world(monkeypatch):
    world = LedgerWorld().install(monkeypatch)
    monkeypatch.setattr(
        "app.services.assaf_os.AssafOsPort",
        lambda *_a, **_k: pytest.fail("the ledger path must never build the Apps Script port"),
    )
    return world


def _prepare(store, world, **changes):
    values = dict(
        settings=world.settings,
        principal=PRINCIPAL,
        contact_id="crm-contact-1",
        conversation_id="site-current",
        path="לימוד",
        summary="",
        next_step="",
    )
    values.update(changes)
    return prepare_selected_crm_lead(store, **values)


def _final_rows(store) -> list[ApprovalRow]:
    rows = store.session.query(ApprovalRow).all()
    return [row for row in rows if (read_owner_action(row) or {}).get("kind") == FINAL_KIND]


def test_the_lead_request_id_is_a_mia_request_uuid_of_the_website_lead_source(store, world):
    seed_lead_source(store)
    proposal = _prepare(store, world)
    stable = assaf_os_leads._digest(
        {"owner": OWNER, "workspace": WORKSPACE, "contact_id": "crm-contact-1"}
    )
    hex32 = assaf_os_leads._digest({"stable": stable, "attempt": 0})[:32]
    expected = api.mia_request_uuid(OWNER, WORKSPACE, f"website-lead-{hex32}")
    assert [call[2] for call in world.ledger.named("propose")] == [expected]
    (row,) = world.ledger.rows("os_proposals")
    assert row["request_id"] == expected and row["source"] == "MIA"
    envelope = read_owner_action(store.get_approval_by_approval_id(proposal.approval_id))
    assert is_ledger_owner_action(envelope)
    assert envelope["parameters"]["request_id"] == row["approval_id"]
    assert envelope["target"]["proposal"]["request_id"] == expected
    assert envelope["target"]["source_binding"]["contact_id"] == "crm-contact-1"
    assert world.receipts() == []  # preparing never writes a lead


def test_the_duplicate_pre_check_is_a_ledger_read_and_writes_nothing(store, world):
    world.commit_lead(source_id="earlier", phone="+972502222222", email="other@example.com")
    seed_lead_source(store)
    before = len(world.ledger.rows("os_proposals"))
    proposes = len(world.ledger.named("propose"))
    with pytest.raises(ValueError, match="lead_already_exists"):
        _prepare(store, world)
    assert world.ledger.named("page")  # the pre-check read the ledger
    assert len(world.ledger.named("propose")) == proposes  # and never proposed
    assert len(world.ledger.rows("os_proposals")) == before
    assert _final_rows(store) == []
    assert all(status != "pending_review" for _, status in provider_claims(store))


def test_a_ledger_lead_already_exists_at_propose_is_a_definite_no_write(store, world, monkeypatch):
    # The pre-check races a commit: the read sees nothing, the ledger guard refuses.
    world.commit_lead(source_id="racing", phone="+972502222222", email="")
    monkeypatch.setattr(assaf_os_leads, "_ledger_lead_exists", lambda *_a, **_k: False)
    seed_lead_source(store)
    with pytest.raises(ValueError, match="lead_already_exists"):
        _prepare(store, world)
    assert _final_rows(store) == []
    claims = provider_claims(store)
    assert all(status != "pending_review" for _, status in claims)
    assert not any(status == "provider_claimed" for _, status in claims)


def test_a_definite_ledger_refusal_releases_the_preparation_claim(store, world):
    seed_lead_source(store)
    world.settings = pg_settings(assaf_os_freeze=True)
    with pytest.raises(AssafOsError) as refused:
        _prepare(store, world, settings=world.settings)
    assert refused.value.code == "os_frozen"
    assert provider_claims(store) == []
    world.settings = pg_settings()
    assert _prepare(store, world).created is True


def test_an_unknown_propose_outcome_stays_uncertain(store, world):
    seed_lead_source(store)
    world.ledger.inject("commit_unknown", RuntimeError("lost"))
    with pytest.raises(AssafOsError) as uncertain:
        _prepare(store, world)
    assert uncertain.value.code == "unknown"
    assert [status for _, status in provider_claims(store)] == ["pending_review"]


def test_owner_path_choice_prepares_a_ledger_card_and_maps_ledger_errors(store, world):
    payload = seed_lead_source(store)
    prepared = prepare_lead_choice(
        store, settings=world.settings, recipient_id=OWNER, payload=payload
    )
    world.settings = pg_settings(kill_switch=True)
    refused = choose_lead_path(
        store, settings=world.settings, principal=PRINCIPAL,
        approval_id=prepared.proposal.approval_id, path="לימוד",
    )
    assert refused.status == "kill_switch"
    world.settings = pg_settings()
    chosen = choose_lead_path(
        store, settings=world.settings, principal=PRINCIPAL,
        approval_id=prepared.proposal.approval_id, path="לימוד",
    )
    assert chosen.status == "prepared"
    assert world.receipts() == []


def test_the_lead_card_is_approved_once_and_a_second_lead_is_refused(store, world):
    seed_lead_source(store)
    proposal = _prepare(store, world)
    approve_row(store, proposal.approval_id)
    outcome = execute_approved_owner_action_with_adapters(
        store, settings=world.settings, principal=PRINCIPAL, proposal_id=proposal.proposal_id
    )
    assert outcome.status == "executed"
    assert len(world.ledger.rows("os_leads")) == 1
    replay = execute_approved_owner_action_with_adapters(
        store, settings=world.settings, principal=PRINCIPAL, proposal_id=proposal.proposal_id
    )
    assert replay.status == "already_handled"
    assert len(world.receipts()) == 1


def test_a_changed_source_refuses_the_decide_and_marks_the_card_terminal(store, world):
    seed_lead_source(store)
    proposal = _prepare(store, world)
    conversation = store.session.get(
        assaf_os_leads.CrmContactConversationRow, "conversation-link-1"
    )
    captured = json.loads(conversation.captured_fields_json)
    conversation.captured_fields_json = json.dumps({**captured, "name": "אחר"}, ensure_ascii=False)
    approve_row(store, proposal.approval_id)
    outcome = execute_approved_owner_action_with_adapters(
        store, settings=world.settings, principal=PRINCIPAL, proposal_id=proposal.proposal_id
    )
    assert outcome.status == "target_changed"
    assert world.ledger.named("decide") == []
    row = store.get_approval_by_approval_id(proposal.approval_id)
    assert row.result == "terminal:target_changed"


def test_the_mia_lead_path_never_reaches_decide(store, world):
    payload = seed_lead_source(store)
    prepared = prepare_lead_choice(
        store, settings=world.settings, recipient_id=OWNER, payload=payload
    )
    choose_lead_path(
        store, settings=world.settings, principal=PRINCIPAL,
        approval_id=prepared.proposal.approval_id, path="אוטומציה",
    )
    assert world.ledger.named("decide") == []
    assert {call[1] for call in world.ledger.calls} <= {"MIA"}
    assert all(name == "from_mia_tool" for name, _ in world.authorize.calls)


def _calls_and_imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.name for alias in node.names)
    return names


def test_no_mia_tool_or_lead_service_symbol_reaches_decide_or_commit():
    forbidden = {
        "decide", "from_telegram_decision", "execute_ledger_owner_decision",
        "execute_approved_owner_action_with_adapters", "reject_assaf_os_owner_action",
        "commit_direct", "owner_direct", "decide_owner_action",
    }
    sources = sorted((REPO / "app" / "tools" / "owner").glob("*.py"))
    sources.append(REPO / "app" / "services" / "assaf_os_leads.py")
    for path in sources:
        hits = _calls_and_imports(path) & forbidden
        assert not hits, f"{path.name} references {sorted(hits)}"


def test_expired_ledger_lead_recovery_needs_receipt_proof(store, world):
    seed_lead_source(store)
    first = _prepare(store, world)
    old = approve_row(store, first.approval_id)
    world.ledger.advance(timedelta(minutes=11).total_seconds())
    fresh = recover_expired_approved_crm_lead(
        store, settings=world.settings, principal=PRINCIPAL, approval_id=first.approval_id,
        path="לימוד", summary="fresh review", next_step="",
    )
    assert fresh.created is True and fresh.approval_id != first.approval_id
    assert old.result == "terminal:remote_expired_no_receipt"
    assert world.receipts() == []


def test_expired_ledger_lead_recovery_refuses_a_committed_receipt(store, world):
    seed_lead_source(store)
    first = _prepare(store, world)
    approve_row(store, first.approval_id)
    envelope = read_owner_action(store.get_approval_by_approval_id(first.approval_id))
    params = envelope["parameters"]
    world.ledger.decide(
        world.ctx("OS"), params["proposal_id"], params["request_id"], "approve"
    )
    with pytest.raises(ValueError, match="remote_no_write_unproven"):
        recover_expired_approved_crm_lead(
            store, settings=world.settings, principal=PRINCIPAL,
            approval_id=first.approval_id, path="לימוד", summary="", next_step="",
        )


@pytest.mark.parametrize(
    ("enabled", "backend", "expected"),
    [
        (False, "sheets", True),
        (True, "sheets", False),
        (False, "postgres", False),
        (True, "postgres", False),
    ],
)
def test_legacy_sheet_projection_is_never_on_under_the_ledger(enabled, backend, expected):
    settings = pg_settings(assaf_os_enabled=enabled, assaf_os_backend=backend)
    assert legacy_sheet_projection_enabled(settings) is expected


def test_crm_runtime_wires_the_reviewed_projection_flag(monkeypatch):
    from app.workers import crm_runtime

    seen = {}

    class Worker:
        def __init__(self, **kwargs):
            seen.update(kwargs)

        def run_once(self):
            return None

    monkeypatch.setattr(crm_runtime, "CrmDeliveryWorker", Worker)
    monkeypatch.setattr(crm_runtime, "build_sheets_port", lambda settings: object())
    settings = pg_settings(crm_delivery_enabled=True, assaf_os_enabled=False, kill_switch=True)
    stopped, thread = crm_runtime.start_crm_runtime(settings, session_factory=lambda: None)
    stopped.set()
    thread.join(5)
    assert seen["legacy_sheet_projection_enabled"] is False
