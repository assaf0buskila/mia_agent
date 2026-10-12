from datetime import UTC, datetime

import pytest
from app.capabilities.types import Principal
from app.core.config import MiaEnv, Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.approvals import DECISION_APPROVED
from app.domain.owner.proposal_cards import render_owner_proposal_card
from app.services.owner_actions import (
    decide_owner_action,
    execute_approved_owner_action_with_adapters,
    propose_owner_action,
    read_owner_action,
)
from app.tools.owner.assaf_os import _assaf_os_propose
from app.tools.owner.crm import _crm_conflicts, _crm_search, _crm_upsert
from app.tools.owner.sheets import _sheets_update
from app.tools.owner.types import ToolContext
from app.tools.registries.owner_tools import execute_tool, get_tool, tool_definitions

OWNER = "123456"
REQUEST = "12345678-1234-4234-8234-123456789012"


def settings(**changes):
    values = dict(
        _env_file=None,
        env=MiaEnv.TEST,
        telegram_owner_user_ids=OWNER,
        owner_os_enabled=True,
        assaf_os_enabled=True,
        assaf_os_script_url="https://script.google.com/macros/s/" + "x" * 24 + "/exec",
        assaf_os_workbook_id="workbook_12345678901234567890",
        assaf_os_workspace_id="workspace-test",
        assaf_os_hmac_secret="assaf-os-test-secret-at-least-32-bytes",
        assaf_os_mode="TEST",
    )
    values.update(changes)
    return Settings(**values)


def context(store, **changes):
    values = dict(
        store=store,
        brain=None,
        settings=settings(),
        embedding_port=None,
        principal=Principal.owner(source="telegram", actor_id=OWNER),
        private_owner_chat=True,
        source_ref="tg:update:1",
        owner_text="create Dana dana@example.com",
        now=datetime(2026, 10, 12, 9, tzinfo=UTC),
    )
    values.update(changes)
    return ToolContext(**values)


def pending():
    return {
        "api_version": "assaf-os.v1",
        "source": "google_sheets",
        "actor_source": "MIA",
        "mode": "TEST",
        "workbook_id": "workbook_12345678901234567890",
        "workspace_id": "workspace-test",
        "status": "pending",
        "proposal_id": "PR_12345678901234567890",
        "request_id": REQUEST,
        "approval_request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "rejection_request_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "expires_at": "2026-10-12T09:10:00+00:00",
        "entity": "clients",
        "record_id": None,
        "expected_revision": 0,
        "input": {"name": "Dana", "email": "dana@example.com", "status": "פעיל"},
        "before": None,
        "after": {"person_id": "P_123", "name": "Dana"},
    }


def test_tools_default_off_and_only_explicit_assaf_os_enable_exposes_them():
    default = {item["function"]["name"] for item in tool_definitions()}
    enabled = {item["function"]["name"] for item in tool_definitions(allow_assaf_os=True)}
    expected = {"assaf_os_snapshot", "assaf_os_receipt", "assaf_os_propose"}
    assert not expected & default
    assert expected <= enabled


def test_assaf_os_mode_removes_every_legacy_owner_business_writer_from_model_tools():
    legacy = {
        item["function"]["name"]
        for item in tool_definitions(allow_business=True, allow_assaf_os=False)
    }
    unified = {
        item["function"]["name"]
        for item in tool_definitions(allow_business=True, allow_assaf_os=True)
    }
    legacy_writes = {
        "crm_upsert",
        "crm_search",
        "crm_record_activity",
        "crm_conflicts",
        "crm_resolve_conflict",
        "sheets_update",
        "sheets_append",
    }
    assert legacy_writes <= legacy
    assert any(name.startswith("business_") for name in legacy)
    assert {"assaf_os_snapshot", "assaf_os_receipt", "assaf_os_propose"} <= unified
    assert not legacy_writes & unified
    assert not any(name.startswith("business_") for name in unified)


@pytest.mark.parametrize(
    "tool_name",
    [
        "crm_upsert",
        "crm_search",
        "crm_record_activity",
        "crm_conflicts",
        "crm_resolve_conflict",
        "sheets_update",
        "sheets_append",
        "business_propose_deal",
        "business_overview",
    ],
)
def test_assaf_os_mode_rejects_legacy_business_tools_before_handler(tool_name):
    result = execute_tool(tool_name, {}, context(None))
    assert not result.ok and result.error == "legacy owner business path is disabled"


def test_nested_legacy_sheet_and_crm_writers_fail_before_provider_access():
    class ExplodingPort:
        def __getattr__(self, _name):
            raise AssertionError("legacy provider touched")

    ctx = context(None, sheets=ExplodingPort())
    assert _sheets_update(ctx, {}).error == "legacy owner Sheets writer is disabled"
    assert _crm_upsert(ctx, {}).error == "legacy owner CRM writer is disabled"


def test_unified_crm_reads_never_touch_sync_provider_but_off_mode_keeps_baseline():
    class TrapSheets:
        def __init__(self):
            self.list_calls = 0
            self.ensure_calls = 0
            self.read_calls = 0

        def list_sheet_names(self, **_kwargs):
            self.list_calls += 1
            return ["Contacts", "Activity"]

        def ensure_crm_workspace(self):
            self.ensure_calls += 1

        def read_crm_contacts_chunk(self, **_kwargs):
            self.read_calls += 1
            return []

    unified_trap = TrapSheets()
    unified_ctx = context(None, sheets=unified_trap)
    assert _crm_search(unified_ctx, {"query": "Dana"}).error == (
        "legacy owner CRM path is disabled"
    )
    assert _crm_conflicts(unified_ctx, {}).error == "legacy owner CRM path is disabled"
    assert (
        unified_trap.list_calls,
        unified_trap.ensure_calls,
        unified_trap.read_calls,
    ) == (0, 0, 0)

    init_db()
    session = get_session_factory()()
    try:
        legacy_trap = TrapSheets()
        legacy_ctx = context(
            LeadStore(session),
            settings=settings(assaf_os_enabled=False),
            sheets=legacy_trap,
            owner_text="find Dana",
        )
        result = _crm_search(
            legacy_ctx,
            {"query": "NoSuchContactRoutingBaselineXZ"},
        )
        assert result.ok and result.text == "No CRM contact matched."
        assert legacy_trap.ensure_calls == 1 and legacy_trap.read_calls == 1
    finally:
        session.close()


def test_ads_reporting_is_conditional_read_only_capability_in_tool_contract():
    search = get_tool("composio_search_tools")
    execute = get_tool("composio_execute_tool")
    assert search and "actually connected" in search.description
    assert execute and "advertising reporting" in execute.description
    assert "Meta Ads mutations are prohibited" in execute.description
    assert "never auto-imported" in execute.description


def test_model_contact_must_be_present_in_current_owner_text(monkeypatch):
    class ExplodingPort:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("provider touched")

    monkeypatch.setattr("app.tools.owner.assaf_os.AssafOsPort", ExplodingPort)
    result = _assaf_os_propose(
        context(None, owner_text="create Dana"),
        {
            "request_id": REQUEST,
            "entity": "clients",
            "record_id": None,
            "expected_revision": 0,
            "input_json": '{"name":"Dana","email":"fabricated@example.com","status":"פעיל"}',
        },
    )
    assert not result.ok and "current owner message" in result.error


def test_model_contact_provenance_rejects_substring_collision(monkeypatch):
    class ExplodingPort:
        def __init__(self, *_args, **_kwargs):
            raise AssertionError("provider touched")

    monkeypatch.setattr("app.tools.owner.assaf_os.AssafOsPort", ExplodingPort)
    result = _assaf_os_propose(
        context(None, owner_text="create notdana@example.com or 105012345678"),
        {
            "request_id": REQUEST,
            "entity": "clients",
            "record_id": None,
            "expected_revision": 0,
            "input_json": '{"name":"Dana","email":"dana@example.com","phone":"05012345678"}',
        },
    )
    assert not result.ok and "current owner message" in result.error


def test_assaf_os_card_formats_money_and_keeps_complete_human_before_after():
    long_note = "א" * 350
    envelope = {
        "kind": "assaf_os.approve",
        "parameters": {"proposal_id": "PR_123"},
        "target": {
            "proposal": {
                "entity": "expenses",
                "record_id": "E_123",
                "expected_revision": 2,
                "expires_at": "2026-10-12T09:10:00Z",
                "input": {"amount_ils": 12345, "notes": long_note},
                "before": {"amount_ils": 10000, "notes": "ישן", "currency": "ILS"},
                "after": {"amount_ils": 12345, "notes": long_note, "currency": "ILS"},
            }
        },
    }
    card = render_owner_proposal_card(envelope)
    assert "סכום" in card and "100.00 ₪" in card and "123.45 ₪" in card
    assert "הערות" in card and long_note in card and "amount_ils" not in card


def test_assaf_os_card_uses_each_side_currency_during_currency_change():
    envelope = {
        "kind": "assaf_os.approve",
        "parameters": {"proposal_id": "PR_123"},
        "target": {
            "proposal": {
                "entity": "expenses",
                "record_id": "E_123",
                "expected_revision": 2,
                "expires_at": "2026-10-12T09:10:00Z",
                "input": {"amount_ils": 12345, "currency": "ILS"},
                "before": {"amount_ils": 10000, "currency": "USD"},
                "after": {"amount_ils": 12345, "currency": "ILS"},
            }
        },
    }
    card = render_owner_proposal_card(envelope)
    assert "100.00 USD" in card and "123.45 ₪" in card


def test_promotion_card_shows_human_and_client_identity():
    value = pending()
    value.update(
        entity="promote_lead",
        record_id="L_123",
        input={},
        before={"lead_id": "L_123", "person_id": "P_123", "name": "דנה"},
        after={
            "lead": {"lead_id": "L_123", "person_id": "P_123", "name": "דנה"},
            "client": {
                "person_id": "P_123",
                "name": "דנה",
                "email": "dana@example.com",
                "status": "פעיל",
            },
        },
    )
    card = render_owner_proposal_card(
        {
            "kind": "assaf_os.approve",
            "parameters": {"proposal_id": value["proposal_id"]},
            "target": {"proposal": value},
        }
    )
    assert "הפיכת ליד ללקוח" in card
    assert "P_123" in card and "L_123" in card and "dana@example.com" in card and "דנה" in card


def test_unreviewable_oversized_card_never_creates_owner_approval(monkeypatch):
    too_large = "א" * 2_000

    class FakePort:
        def __init__(self, *_args, **_kwargs):
            pass

        def propose(self, _request):
            value = pending()
            value.update(
                entity="tasks",
                input={"title": too_large, "notes": too_large},
                before=None,
                after={"task_id": "T_1", "title": too_large, "notes": too_large},
            )
            return value

    monkeypatch.setattr("app.tools.owner.assaf_os.AssafOsPort", FakePort)
    result = _assaf_os_propose(
        context(None),
        {
            "request_id": REQUEST,
            "entity": "tasks",
            "record_id": None,
            "expected_revision": 0,
            "input_json": __import__("json").dumps(
                {"title": too_large, "notes": too_large}, ensure_ascii=False
            ),
        },
    )
    assert not result.ok and result.error == "proposal_unreviewable"


def test_mia_proposal_uses_script_ids_and_creates_exact_ten_minute_telegram_card(monkeypatch):
    init_db()
    session = get_session_factory()()
    store = LeadStore(session)

    class FakePort:
        def __init__(self, *_args, **kwargs):
            assert kwargs["actor_source"] == "MIA"

        def propose(self, _request):
            return pending()

    monkeypatch.setattr("app.tools.owner.assaf_os.AssafOsPort", FakePort)
    try:
        result = _assaf_os_propose(
            context(store),
            {
                "request_id": REQUEST,
                "entity": "clients",
                "record_id": None,
                "expected_revision": 0,
                "input_json": '{"name":"Dana","email":"dana@example.com","status":"פעיל"}',
            },
        )
        assert result.ok and result.approval_id
        row = store.get_approval_by_approval_id(result.approval_id)
        envelope = read_owner_action(row)
        assert envelope["parameters"] == {
            "proposal_id": "PR_12345678901234567890",
            "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "rejection_request_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        }
        assert row.expires_at == "2026-10-12T09:10:00+00:00"
        card = render_owner_proposal_card(envelope)
        assert "Dana" in card and "dana@example.com" in card and "PR_12345678901234567890" in card
    finally:
        session.close()


def test_exact_script_approval_id_is_committed_once_after_telegram_decision(monkeypatch):
    init_db()
    session = get_session_factory()()
    store = LeadStore(session)
    actor = Principal.owner(source="telegram", actor_id=OWNER)

    class FakePort:
        calls = []

        def __init__(self, *_args, **kwargs):
            self.source = kwargs["actor_source"]

        def propose(self, _request):
            return pending()

        def proposal(self, proposal_id):
            assert self.source == "MIA" and proposal_id == pending()["proposal_id"]
            return pending()

        def decide(self, operation, proposal_id, request):
            self.calls.append((self.source, operation, proposal_id, request.request_id))
            return {
                "status": "committed",
                "proposal_id": proposal_id,
            }

    monkeypatch.setattr("app.tools.owner.assaf_os.AssafOsPort", FakePort)
    monkeypatch.setattr("app.services.assaf_os.AssafOsPort", FakePort)
    monkeypatch.setattr(
        "app.services.owner_actions._aware",
        lambda value: value or datetime(2026, 10, 12, 9, 2, tzinfo=UTC),
    )
    try:
        proposed = _assaf_os_propose(
            context(store),
            {
                "request_id": REQUEST,
                "entity": "clients",
                "record_id": None,
                "expected_revision": 0,
                "input_json": ('{"name":"Dana","email":"dana@example.com","status":"פעיל"}'),
            },
        )
        decision = decide_owner_action(
            store,
            principal=actor,
            approval_id=proposed.approval_id,
            decision=DECISION_APPROVED,
            now=datetime(2026, 10, 12, 9, 1, tzinfo=UTC),
        )
        first = execute_approved_owner_action_with_adapters(
            store,
            settings=settings(),
            principal=actor,
            proposal_id=decision.proposal_id,
        )
        second = execute_approved_owner_action_with_adapters(
            store,
            settings=settings(),
            principal=actor,
            proposal_id=decision.proposal_id,
        )
        assert first.status == "executed" and second.status == "already_handled"
        assert FakePort.calls == [
            (
                "TELEGRAM_APPROVAL",
                "approve",
                pending()["proposal_id"],
                pending()["approval_request_id"],
            )
        ]
    finally:
        session.close()


@pytest.mark.parametrize("terminal", ["expired", "target_changed", "rejected"])
def test_terminal_script_decision_is_never_reported_as_committed(monkeypatch, terminal):
    init_db()
    session = get_session_factory()()
    store = LeadStore(session)
    actor = Principal.owner(source="telegram", actor_id=OWNER)

    class FakePort:
        def __init__(self, *_args, **kwargs):
            self.source = kwargs["actor_source"]

        def propose(self, _request):
            return pending()

        def proposal(self, _proposal_id):
            return pending()

        def decide(self, _operation, proposal_id, _request):
            return {"status": terminal, "proposal_id": proposal_id}

    monkeypatch.setattr("app.tools.owner.assaf_os.AssafOsPort", FakePort)
    monkeypatch.setattr("app.services.assaf_os.AssafOsPort", FakePort)
    monkeypatch.setattr(
        "app.services.owner_actions._aware",
        lambda value: value or datetime(2026, 10, 12, 9, 2, tzinfo=UTC),
    )
    try:
        proposed = _assaf_os_propose(
            context(store, source_ref=f"tg:terminal:{terminal}"),
            {
                "request_id": REQUEST,
                "entity": "clients",
                "record_id": None,
                "expected_revision": 0,
                "input_json": '{"name":"Dana","email":"dana@example.com","status":"פעיל"}',
            },
        )
        decision = decide_owner_action(
            store,
            principal=actor,
            approval_id=proposed.approval_id,
            decision=DECISION_APPROVED,
            now=datetime(2026, 10, 12, 9, 1, tzinfo=UTC),
        )
        outcome = execute_approved_owner_action_with_adapters(
            store,
            settings=settings(),
            principal=actor,
            proposal_id=decision.proposal_id,
        )
        assert outcome.status == terminal and not outcome.text
        row = store.get_approval_by_resource(
            "owner_proposal", decision.proposal_id, "owner_external_write"
        )
        assert not row.executed_at
    finally:
        session.close()


def test_kill_switch_blocks_telegram_approval_before_port_construction(monkeypatch):
    init_db()
    session = get_session_factory()()
    store = LeadStore(session)
    actor = Principal.owner(source="telegram", actor_id=OWNER)

    class ProposalPort:
        def __init__(self, *_args, **_kwargs):
            pass

        def propose(self, _request):
            return pending()

    monkeypatch.setattr("app.tools.owner.assaf_os.AssafOsPort", ProposalPort)
    try:
        proposed = _assaf_os_propose(
            context(store, source_ref="tg:kill:1"),
            {
                "request_id": REQUEST,
                "entity": "clients",
                "record_id": None,
                "expected_revision": 0,
                "input_json": '{"name":"Dana","email":"dana@example.com","status":"פעיל"}',
            },
        )
        decision = decide_owner_action(
            store,
            principal=actor,
            approval_id=proposed.approval_id,
            decision=DECISION_APPROVED,
            now=datetime(2026, 10, 12, 9, 1, tzinfo=UTC),
        )

        class ExplodingPort:
            def __init__(self, *_args, **_kwargs):
                raise AssertionError("provider port constructed")

        monkeypatch.setattr("app.services.assaf_os.AssafOsPort", ExplodingPort)
        monkeypatch.setattr(
            "app.services.owner_actions._aware",
            lambda value: value or datetime(2026, 10, 12, 9, 2, tzinfo=UTC),
        )
        outcome = execute_approved_owner_action_with_adapters(
            store,
            settings=settings(kill_switch=True),
            principal=actor,
            proposal_id=decision.proposal_id,
        )
        assert outcome.status == "policy_denied"
    finally:
        session.close()


def test_enabling_assaf_os_blocks_preexisting_legacy_crm_approval_before_provider(monkeypatch):
    init_db()
    session = get_session_factory()()
    store = LeadStore(session)
    actor = Principal.owner(source="telegram", actor_id=OWNER)
    try:
        proposal = propose_owner_action(
            store,
            principal=actor,
            source_ref="tg:legacy-before-unified",
            kind="crm.upsert",
            parameters={"fields": {}, "contact_id": "crm_1", "expected_revision": 1},
            target={},
            now=datetime(2026, 10, 12, 9, tzinfo=UTC),
        )
        decision = decide_owner_action(
            store,
            principal=actor,
            approval_id=proposal.approval_id,
            decision=DECISION_APPROVED,
            now=datetime(2026, 10, 12, 9, 1, tzinfo=UTC),
        )
        monkeypatch.setattr(
            "app.services.owner_actions._aware",
            lambda value: value or datetime(2026, 10, 12, 9, 2, tzinfo=UTC),
        )
        monkeypatch.setattr(
            "app.integrations.sheets.build_sheets_port",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("legacy provider constructed")
            ),
        )
        outcome = execute_approved_owner_action_with_adapters(
            store,
            settings=settings(assaf_os_enabled=True),
            principal=actor,
            proposal_id=decision.proposal_id,
        )
        assert outcome.status == "policy_denied"
    finally:
        session.close()
