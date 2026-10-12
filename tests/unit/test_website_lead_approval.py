from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from app.capabilities.types import Principal
from app.core.config import MiaEnv, Settings
from app.db.base import Base
from app.db.models import (
    CrmActivityRow,
    CrmContactConversationRow,
    CrmContactRow,
    CrmOutboxRow,
)
from app.db.session import make_engine
from app.db.store import LeadStore
from app.domain.approvals import DECISION_APPROVED
from app.domain.owner.uncertain_writes import describe_stuck_write
from app.integrations.llm_client import OPENAI_RESPONSES_URL, LlmClient, LlmError
from app.integrations.telegram_format import plain_text_length
from app.services import assaf_os_leads
from app.services.assaf_os import AssafOsError
from app.services.assaf_os_leads import (
    choose_lead_path,
    current_lead_duplicate_guard,
    model_lead_template,
    prepare_lead_choice,
    prepare_selected_crm_lead,
    source_from_delivery_payload,
)
from app.services.owner_actions import (
    execute_approved_owner_action_with_adapters,
    propose_owner_action,
    read_owner_action,
)
from app.tools.owner.assaf_os import _contact_provenance
from app.tools.owner.types import ToolContext
from app.workers.crm_delivery import CrmDeliveryWorker
from app.workers.crm_runtime import telegram_receipt_handler
from sqlalchemy.orm import sessionmaker

OWNER = "123456"
NOW = datetime.now(UTC).isoformat()


def _settings(**changes) -> Settings:
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


@pytest.fixture
def lead_store(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'lead-approval.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    session = factory()
    store = LeadStore(session)
    yield store
    session.close()
    engine.dispose()


def _seed_source(
    store: LeadStore,
    *,
    contact_fields: dict | None = None,
    captured_fields: dict | None = None,
    raw_fields: dict | None = None,
):
    contact = contact_fields or {
        "name": "שם קנוני פרטי",
        "phone": "+972501111111",
        "email": "canonical@example.com",
        "source": "older",
        "status": "new",
    }
    captured = captured_fields or {
        "name": "דנה",
        "phone": "+972502222222",
        "email": "visitor@example.com",
        "source": "AssafWeb",
        "status": "new",
    }
    raw = raw_fields or {
        "name": "דנה",
        "phone": "050-222-2222",
        "email": "visitor@example.com",
        "source": "AssafWeb",
        "status": "new",
    }
    store.session.add(
        CrmContactRow(
            id="crm-contact-1",
            revision=1,
            fields_json=json.dumps(contact, ensure_ascii=False),
            source_ref="old-source",
            conversation_id="older-conversation",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    store.session.add(
        CrmContactConversationRow(
            id="conversation-link-1",
            contact_id="crm-contact-1",
            conversation_id="site-current",
            captured_fields_json=json.dumps(captured, ensure_ascii=False),
            source_ref="website:site-current",
            created_at=NOW,
            updated_at=NOW,
        )
    )
    payload = {
        "conversation_id": "site-current",
        "recipient_id": OWNER,
        "text": "new lead",
        "lead_source": {"source_ref": "website:site-current", "raw_fields": raw},
    }
    store.session.add(
        CrmOutboxRow(
            id="outbox-1",
            dedupe_key="telegram:site-current",
            aggregate_type="contact",
            aggregate_id="crm-contact-1",
            destination="telegram",
            payload_json=json.dumps(payload, ensure_ascii=False),
            status="pending",
            created_at=NOW,
        )
    )
    store.session.flush()
    return payload


def _pending(lead_input: dict[str, str]) -> dict:
    return {
        "api_version": "assaf-os.v1",
        "source": "google_sheets",
        "actor_source": "MIA",
        "mode": "TEST",
        "workbook_id": "workbook_12345678901234567890",
        "workspace_id": "workspace-test",
        "status": "pending",
        "proposal_id": "PR_12345678901234567890",
        "request_id": "website-lead-request",
        "approval_request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        "rejection_request_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
        "expires_at": "2099-10-12T09:10:00+00:00",
        "entity": "leads",
        "record_id": None,
        "expected_revision": 0,
        "input": lead_input,
        "before": None,
        "after": {"lead_id": "L_123", **lead_input},
    }


class FakeAssafOsPort:
    proposals: list[object] = []
    snapshot_rows: list[dict] = []
    current_proposal: dict = {}
    decisions: list[object] = []

    def __init__(self, *_args, **_kwargs):
        pass

    def snapshot(self):
        return {"records": {"leads": list(self.snapshot_rows)}}

    def propose(self, request):
        self.proposals.append(request)
        self.current_proposal = _pending(request.input)
        type(self).current_proposal = self.current_proposal
        return self.current_proposal

    def proposal(self, _proposal_id):
        return dict(type(self).current_proposal)

    def decide(self, *_args):
        type(self).decisions.append(_args)
        return {"status": "committed", "proposal_id": self.current_proposal["proposal_id"]}


def test_public_source_is_conversation_scoped_and_keeps_raw_phone_reviewable(lead_store):
    payload = _seed_source(lead_store)
    source = source_from_delivery_payload(lead_store, payload)
    assert source["captured"]["name"] == "דנה"
    assert source["captured"]["email"] == "visitor@example.com"
    assert source["raw_fields"]["phone"] == "050-222-2222"
    assert "canonical@example.com" not in json.dumps(source, ensure_ascii=False)


def test_model_contact_invention_is_rejected_and_timeout_uses_visible_fallback(lead_store):
    source = source_from_delivery_payload(lead_store, _seed_source(lead_store))

    class Response:
        text = json.dumps(
            {
                "summary": "סיכום",
                "next_step": "לחזור",
                "recommended_path": "לימוד",
                "missing_fields": [],
                "phone": "0509999999",
            },
            ensure_ascii=False,
        )
        tool_calls = []

        def truncated(self):
            return False

        def refused(self):
            return False

    class InventingClient:
        def enabled(self):
            return True

        def complete(self, **_kwargs):
            return Response()

    class TimeoutClient(InventingClient):
        def complete(self, **_kwargs):
            raise LlmError("provider timeout")

    invented = model_lead_template(source, InventingClient())
    timed_out = model_lead_template(source, TimeoutClient())
    assert invented.summary == ""
    assert timed_out.recommended_path == "unknown"

    Response.text = json.dumps(
        {
            "summary": "לבקש מדנה לחזור ל-0509999999",
            "next_step": "לכתוב ל-invented@example.com",
            "recommended_path": "אוטומציה",
            "missing_fields": [],
        },
        ensure_ascii=False,
    )
    embedded_contact = model_lead_template(source, InventingClient())
    assert embedded_contact.summary == ""
    assert embedded_contact.recommended_path == "unknown"


def test_model_template_uses_concrete_responses_contract_and_redacts_contacts(lead_store):
    source = source_from_delivery_payload(lead_store, _seed_source(lead_store))
    source["captured"]["summary"] = "חזרו אלי ל050-777-7777 או לhidden@example.com"
    source["activity_summary"] = "טלפון+972507777777 ודואללother@example.com"

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        serialized = json.dumps(payload, ensure_ascii=False)
        assert "050-777-7777" not in serialized
        assert "+972507777777" not in serialized
        assert "hidden@example.com" not in serialized
        assert "other@example.com" not in serialized
        assert payload["max_output_tokens"] == 240
        assert payload["text"]["format"]["type"] == "json_schema"
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(
                                    {
                                        "summary": "בקשה לאוטומציה",
                                        "next_step": "לברר תהליך",
                                        "recommended_path": "אוטומציה",
                                        "missing_fields": [],
                                    },
                                    ensure_ascii=False,
                                ),
                            }
                        ],
                    }
                ],
                "usage": {"input_tokens": 20, "output_tokens": 10},
            },
        )

    http = httpx.Client(transport=httpx.MockTransport(handler))
    client = LlmClient(
        api_key="not-a-real-key",
        model="configured-owner-model",
        url=OPENAI_RESPONSES_URL,
        client=http,
    )
    try:
        template = model_lead_template(source, client)
    finally:
        http.close()
    assert template.summary == "בקשה לאוטומציה"
    assert template.recommended_path == "אוטומציה"


def test_model_rejects_contact_attached_to_hebrew_prefix(lead_store):
    source = source_from_delivery_payload(lead_store, _seed_source(lead_store))

    class Response:
        text = json.dumps(
            {
                "summary": "אפשר לחזור ל0501234567",
                "next_step": "כתבו לdana@example.com",
                "recommended_path": "אוטומציה",
                "missing_fields": [],
            },
            ensure_ascii=False,
        )
        tool_calls = []

        def truncated(self):
            return False

        def refused(self):
            return False

    class Client:
        def enabled(self):
            return True

        def complete(self, **_kwargs):
            return Response()

    template = model_lead_template(source, Client())
    assert template.summary == ""
    assert template.recommended_path == "unknown"


@pytest.mark.parametrize("want", ["AI לעסק", "עסק", "לימוד וגם אוטומציה"])
def test_deterministic_fallback_does_not_guess_ambiguous_path(lead_store, want):
    source = source_from_delivery_payload(lead_store, _seed_source(lead_store))
    source["captured"]["want"] = want
    assert model_lead_template(source, None).recommended_path == "unknown"


def test_missing_name_and_malformed_phone_block_path_choice(lead_store):
    payload = _seed_source(
        lead_store,
        captured_fields={"email": "visitor@example.com", "source": "AssafWeb"},
        raw_fields={"phone": "05012", "email": "visitor@example.com"},
    )
    prepared = prepare_lead_choice(
        lead_store, settings=_settings(), recipient_id=OWNER, payload=payload
    )
    assert prepared.blocked
    choice = choose_lead_path(
        lead_store,
        settings=_settings(),
        principal=Principal.owner(source="telegram", actor_id=OWNER),
        approval_id=prepared.proposal.approval_id,
        path="לימוד",
    )
    assert choice.status == "needs_correction"
    assert not FakeAssafOsPort.proposals


def test_path_choice_prepares_once_without_writing_and_binds_exact_schema(
    lead_store, monkeypatch
):
    FakeAssafOsPort.proposals = []
    FakeAssafOsPort.snapshot_rows = []
    monkeypatch.setattr(assaf_os_leads, "AssafOsPort", FakeAssafOsPort)
    prepared = prepare_lead_choice(
        lead_store,
        settings=_settings(),
        recipient_id=OWNER,
        payload=_seed_source(lead_store),
    )
    principal = Principal.owner(source="telegram", actor_id=OWNER)
    first = choose_lead_path(
        lead_store,
        settings=_settings(),
        principal=principal,
        approval_id=prepared.proposal.approval_id,
        path="אוטומציה",
    )
    replay = choose_lead_path(
        lead_store,
        settings=_settings(),
        principal=principal,
        approval_id=prepared.proposal.approval_id,
        path="לימוד",
    )
    assert first.status == "prepared"
    assert replay.status == "already_handled"
    assert len(FakeAssafOsPort.proposals) == 1
    lead_input = FakeAssafOsPort.proposals[0].input
    assert lead_input == {
        "name": "דנה",
        "phone": "+972502222222",
        "email": "visitor@example.com",
        "path": "אוטומציה",
        "status": "חדש",
        "source_note": "AssafWeb",
    }
    final = read_owner_action(lead_store.get_approval_by_approval_id(first.approval_id))
    assert final["kind"] == "assaf_os.approve"
    assert final["target"]["duplicate_guard"]["absent"] is True
    assert final["target"]["contact_provenance"]["raw_phone"] == "050-222-2222"


def test_late_public_brief_refresh_does_not_stale_captured_source_binding(
    lead_store, monkeypatch
):
    from app.services import assaf_os as assaf_os_service

    FakeAssafOsPort.proposals = []
    FakeAssafOsPort.decisions = []
    FakeAssafOsPort.snapshot_rows = []
    monkeypatch.setattr(assaf_os_leads, "AssafOsPort", FakeAssafOsPort)
    monkeypatch.setattr(assaf_os_service, "AssafOsPort", FakeAssafOsPort)
    payload = _seed_source(lead_store)
    lead_store.session.add(
        CrmActivityRow(
            id="activity-late",
            contact_id="crm-contact-1",
            channel="website",
            action="contact_captured",
            result="brief before public model",
            occurred_at=NOW,
            source_ref="website:site-current:activity",
            created_at=NOW,
        )
    )
    lead_store.session.flush()
    prepared = prepare_lead_choice(
        lead_store, settings=_settings(), recipient_id=OWNER, payload=payload
    )
    activity = lead_store.session.get(CrmActivityRow, "activity-late")
    activity.result = "brief refreshed after ping claim"
    lead_store.session.flush()
    principal = Principal.owner(source="telegram", actor_id=OWNER)
    choice = choose_lead_path(
        lead_store,
        settings=_settings(),
        principal=principal,
        approval_id=prepared.proposal.approval_id,
        path="לימוד",
    )
    assert choice.status == "prepared"
    row = lead_store.get_approval_by_approval_id(choice.approval_id)
    row.decision = DECISION_APPROVED
    activity.result = "another derived brief after final review"
    lead_store.session.flush()
    outcome = execute_approved_owner_action_with_adapters(
        lead_store,
        settings=_settings(),
        principal=principal,
        proposal_id=choice.proposal_id,
    )
    assert outcome.status == "executed"


def test_path_choice_rejects_changed_durable_source_before_provider(lead_store, monkeypatch):
    FakeAssafOsPort.proposals = []
    monkeypatch.setattr(assaf_os_leads, "AssafOsPort", FakeAssafOsPort)
    prepared = prepare_lead_choice(
        lead_store,
        settings=_settings(),
        recipient_id=OWNER,
        payload=_seed_source(lead_store),
    )
    job = lead_store.session.get(CrmOutboxRow, "outbox-1")
    changed = json.loads(job.payload_json)
    changed["lead_source"]["raw_fields"]["phone"] = "050-333-3333"
    job.payload_json = json.dumps(changed, ensure_ascii=False)
    lead_store.session.flush()
    result = choose_lead_path(
        lead_store,
        settings=_settings(),
        principal=Principal.owner(source="telegram", actor_id=OWNER),
        approval_id=prepared.proposal.approval_id,
        path="לימוד",
    )
    assert result.status == "source_changed"
    assert FakeAssafOsPort.proposals == []


def test_duplicate_guard_revalidates_native_sheet_state():
    guard = {"phone": "+972502222222", "email": "visitor@example.com", "absent": True}
    assert current_lead_duplicate_guard({"records": {"leads": []}}, guard)["absent"]
    current = {"records": {"leads": [{"phone": "050-222-2222", "email": ""}]}}
    assert current_lead_duplicate_guard(current, guard)["absent"] is False


def test_final_approval_rechecks_sheet_duplicate_before_decide(lead_store, monkeypatch):
    from app.services import assaf_os as assaf_os_service

    FakeAssafOsPort.proposals = []
    FakeAssafOsPort.decisions = []
    FakeAssafOsPort.snapshot_rows = []
    monkeypatch.setattr(assaf_os_leads, "AssafOsPort", FakeAssafOsPort)
    monkeypatch.setattr(assaf_os_service, "AssafOsPort", FakeAssafOsPort)
    _seed_source(lead_store)
    principal = Principal.owner(source="telegram", actor_id=OWNER)
    final = prepare_selected_crm_lead(
        lead_store,
        settings=_settings(),
        principal=principal,
        contact_id="crm-contact-1",
        conversation_id="site-current",
        path="לימוד",
        summary="",
        next_step="",
    )
    row = lead_store.get_approval_by_approval_id(final.approval_id)
    row.decision = DECISION_APPROVED
    FakeAssafOsPort.snapshot_rows = [{"phone": "050-222-2222", "email": ""}]
    outcome = execute_approved_owner_action_with_adapters(
        lead_store,
        settings=_settings(),
        principal=principal,
        proposal_id=final.proposal_id,
    )
    assert outcome.status == "target_changed"
    assert FakeAssafOsPort.decisions == []


def test_final_approval_accepts_unchanged_raw_local_source(lead_store, monkeypatch):
    from app.services import assaf_os as assaf_os_service

    FakeAssafOsPort.proposals = []
    FakeAssafOsPort.decisions = []
    FakeAssafOsPort.snapshot_rows = []
    monkeypatch.setattr(assaf_os_leads, "AssafOsPort", FakeAssafOsPort)
    monkeypatch.setattr(assaf_os_service, "AssafOsPort", FakeAssafOsPort)
    _seed_source(lead_store)
    principal = Principal.owner(source="telegram", actor_id=OWNER)
    final = prepare_selected_crm_lead(
        lead_store,
        settings=_settings(),
        principal=principal,
        contact_id="crm-contact-1",
        conversation_id="site-current",
        path="לימוד",
        summary="",
        next_step="",
    )
    row = lead_store.get_approval_by_approval_id(final.approval_id)
    row.decision = DECISION_APPROVED
    outcome = execute_approved_owner_action_with_adapters(
        lead_store,
        settings=_settings(),
        principal=principal,
        proposal_id=final.proposal_id,
    )
    assert outcome.status == "executed"
    assert len(FakeAssafOsPort.decisions) == 1


def test_conclusive_source_change_allows_a_new_exact_proposal(lead_store, monkeypatch):
    from app.services import assaf_os as assaf_os_service

    FakeAssafOsPort.proposals = []
    FakeAssafOsPort.decisions = []
    FakeAssafOsPort.snapshot_rows = []
    monkeypatch.setattr(assaf_os_leads, "AssafOsPort", FakeAssafOsPort)
    monkeypatch.setattr(assaf_os_service, "AssafOsPort", FakeAssafOsPort)
    _seed_source(lead_store)
    principal = Principal.owner(source="telegram", actor_id=OWNER)
    first = prepare_selected_crm_lead(
        lead_store,
        settings=_settings(),
        principal=principal,
        contact_id="crm-contact-1",
        conversation_id="site-current",
        path="לימוד",
        summary="",
        next_step="",
    )
    lead_store.get_approval_by_approval_id(first.approval_id).decision = DECISION_APPROVED
    conversation = lead_store.session.get(CrmContactConversationRow, "conversation-link-1")
    captured = json.loads(conversation.captured_fields_json)
    captured["name"] = "דנה מתוקנת"
    conversation.captured_fields_json = json.dumps(captured, ensure_ascii=False)
    lead_store.session.flush()
    outcome = execute_approved_owner_action_with_adapters(
        lead_store,
        settings=_settings(),
        principal=principal,
        proposal_id=first.proposal_id,
    )
    assert outcome.status == "target_changed"
    second = prepare_selected_crm_lead(
        lead_store,
        settings=_settings(),
        principal=principal,
        contact_id="crm-contact-1",
        conversation_id="site-current",
        path="אוטומציה",
        summary="updated source",
        next_step="",
    )
    assert second.approval_id != first.approval_id
    assert len(FakeAssafOsPort.proposals) == 2


def test_unknown_execute_cannot_be_reclassified_terminal_or_reprepared(
    lead_store, monkeypatch
):
    from app.services import assaf_os as assaf_os_service

    class UnknownAfterCommitPort(FakeAssafOsPort):
        def decide(self, *_args):
            type(self).current_proposal = {
                **type(self).current_proposal,
                "status": "committed",
            }
            raise AssafOsError("transport lost after commit")

    UnknownAfterCommitPort.proposals = []
    UnknownAfterCommitPort.snapshot_rows = []
    UnknownAfterCommitPort.decisions = []
    monkeypatch.setattr(assaf_os_leads, "AssafOsPort", UnknownAfterCommitPort)
    monkeypatch.setattr(assaf_os_service, "AssafOsPort", UnknownAfterCommitPort)
    _seed_source(lead_store)
    principal = Principal.owner(source="telegram", actor_id=OWNER)
    first = prepare_selected_crm_lead(
        lead_store,
        settings=_settings(),
        principal=principal,
        contact_id="crm-contact-1",
        conversation_id="site-current",
        path="לימוד",
        summary="",
        next_step="",
    )
    row = lead_store.get_approval_by_approval_id(first.approval_id)
    row.decision = DECISION_APPROVED
    first_outcome = execute_approved_owner_action_with_adapters(
        lead_store,
        settings=_settings(),
        principal=principal,
        proposal_id=first.proposal_id,
    )
    replay_outcome = execute_approved_owner_action_with_adapters(
        lead_store,
        settings=_settings(),
        principal=principal,
        proposal_id=first.proposal_id,
    )
    recovered = prepare_selected_crm_lead(
        lead_store,
        settings=_settings(),
        principal=principal,
        contact_id="crm-contact-1",
        conversation_id="site-current",
        path="אוטומציה",
        summary="retry must stay bound",
        next_step="",
    )
    assert first_outcome.status == "unknown"
    assert replay_outcome.status == "target_changed"
    assert not str(row.result or "").startswith("terminal:")
    assert recovered.approval_id == first.approval_id
    assert len(UnknownAfterCommitPort.proposals) == 1


def test_link_created_during_contact_claim_race_wins_without_second_provider_call(
    lead_store, monkeypatch
):
    FakeAssafOsPort.proposals = []
    FakeAssafOsPort.snapshot_rows = []
    monkeypatch.setattr(assaf_os_leads, "AssafOsPort", FakeAssafOsPort)
    source = source_from_delivery_payload(lead_store, _seed_source(lead_store))
    principal = Principal.owner(source="telegram", actor_id=OWNER)
    injected = {}
    original_count = assaf_os_leads._source_proposal_attempt_count
    stable_digest = assaf_os_leads._digest(
        {
            "owner": OWNER,
            "workbook": _settings().assaf_os_workbook_id,
            "contact_id": "crm-contact-1",
        }
    )
    operation_key = f"leadprep-contact:{stable_digest}"
    assert lead_store.claim_provider_write(scope="approval", key=operation_key)
    assert lead_store.complete_provider_write(
        scope="approval", key=operation_key, result_json='{"ok":true}'
    )

    def inject_link(_store, raced_source):
        if injected:
            return original_count(_store, raced_source)
        proposal = propose_owner_action(
            lead_store,
            principal=principal,
            source_ref="race:first",
            kind=assaf_os_leads.FINAL_KIND,
            parameters={
                "proposal_id": "PR_12345678901234567890",
                "request_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                "rejection_request_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            },
            target={"source_binding": assaf_os_leads._source_binding(raced_source)},
            ttl_seconds=600,
        )
        injected["proposal"] = proposal
        return 1

    monkeypatch.setattr(assaf_os_leads, "_source_proposal_attempt_count", inject_link)
    raced = prepare_selected_crm_lead(
        lead_store,
        settings=_settings(),
        principal=principal,
        contact_id=str(source["contact_id"]),
        conversation_id="site-current",
        path="לימוד",
        summary="",
        next_step="",
    )
    assert raced.approval_id == injected["proposal"].approval_id
    assert raced.created is False
    assert FakeAssafOsPort.proposals == []
    assert (
        lead_store.get_provider_write_status(scope="approval", key=operation_key)
        == "completed"
    )
    lead_store.get_approval_by_approval_id(raced.approval_id).expires_at = (
        "2020-01-01T00:00:00+00:00"
    )
    replacement = prepare_selected_crm_lead(
        lead_store,
        settings=_settings(),
        principal=principal,
        contact_id=str(source["contact_id"]),
        conversation_id="site-current",
        path="אוטומציה",
        summary="replacement",
        next_step="",
    )
    assert replacement.approval_id != raced.approval_id
    assert len(FakeAssafOsPort.proposals) == 1


def test_committed_source_link_survives_native_contact_edits(lead_store, monkeypatch):
    FakeAssafOsPort.proposals = []
    FakeAssafOsPort.snapshot_rows = []
    monkeypatch.setattr(assaf_os_leads, "AssafOsPort", FakeAssafOsPort)
    _seed_source(lead_store)
    principal = Principal.owner(source="telegram", actor_id=OWNER)
    first = prepare_selected_crm_lead(
        lead_store,
        settings=_settings(),
        principal=principal,
        contact_id="crm-contact-1",
        conversation_id="site-current",
        path="לימוד",
        summary="",
        next_step="",
    )
    row = lead_store.get_approval_by_approval_id(first.approval_id)
    row.decision = DECISION_APPROVED
    row.executed_at = NOW
    row.result = "Assaf OS committed: PR_12345678901234567890"
    FakeAssafOsPort.snapshot_rows = [
        {"phone": "+972509999999", "email": "native-edited@example.com"}
    ]
    recovered = prepare_selected_crm_lead(
        lead_store,
        settings=_settings(),
        principal=principal,
        contact_id="crm-contact-1",
        conversation_id="site-current",
        path="אוטומציה",
        summary="changed",
        next_step="changed",
    )
    assert recovered.approval_id == first.approval_id
    assert recovered.created is False
    assert len(FakeAssafOsPort.proposals) == 1


def test_provider_uncertainty_sticks_to_stable_contact_claim(lead_store, monkeypatch):
    class UncertainPort(FakeAssafOsPort):
        attempts = 0

        def propose(self, request):
            type(self).attempts += 1
            raise AssafOsError("unknown")

    UncertainPort.snapshot_rows = []
    monkeypatch.setattr(assaf_os_leads, "AssafOsPort", UncertainPort)
    _seed_source(lead_store)
    principal = Principal.owner(source="telegram", actor_id=OWNER)
    with pytest.raises(AssafOsError, match="unknown"):
        prepare_selected_crm_lead(
            lead_store,
            settings=_settings(),
            principal=principal,
            contact_id="crm-contact-1",
            conversation_id="site-current",
            path="לימוד",
            summary="",
            next_step="",
        )
    with pytest.raises(ValueError, match="lead_source_pending_review"):
        prepare_selected_crm_lead(
            lead_store,
            settings=_settings(),
            principal=principal,
            contact_id="crm-contact-1",
            conversation_id="site-current",
            path="אוטומציה",
            summary="second path",
            next_step="",
        )
    assert UncertainPort.attempts == 1
    stuck = lead_store.list_stuck_provider_writes(
        older_than=datetime.now(UTC), limit=10
    )
    assert len(stuck) == 1
    kind, target = describe_stuck_write(
        lead_store, scope=stuck[0].scope, key=stuck[0].key
    )
    context = json.loads(stuck[0].result_json)
    assert kind == "Assaf OS lead proposal"
    assert "CRM crm-contact-1" in target
    assert "receipt request website-lead-" in target
    assert context["contact_id"] == "crm-contact-1"
    assert context["request_id"].startswith("website-lead-")


def test_invalid_source_can_be_corrected_without_poisoning_contact_claim(
    lead_store, monkeypatch
):
    FakeAssafOsPort.proposals = []
    FakeAssafOsPort.snapshot_rows = []
    monkeypatch.setattr(assaf_os_leads, "AssafOsPort", FakeAssafOsPort)
    _seed_source(
        lead_store,
        captured_fields={"phone": "+972502222222", "source": "AssafWeb"},
        raw_fields={"phone": "050-222-2222", "source": "AssafWeb"},
    )
    principal = Principal.owner(source="telegram", actor_id=OWNER)
    with pytest.raises(ValueError, match="missing_name"):
        prepare_selected_crm_lead(
            lead_store,
            settings=_settings(),
            principal=principal,
            contact_id="crm-contact-1",
            conversation_id="site-current",
            path="לימוד",
            summary="",
            next_step="",
        )
    corrected = prepare_selected_crm_lead(
        lead_store,
        settings=_settings(),
        principal=principal,
        contact_id="crm-contact-1",
        conversation_id="site-current",
        path="לימוד",
        summary="",
        next_step="",
        corrected_name="דנה",
    )
    assert corrected.created is True
    assert len(FakeAssafOsPort.proposals) == 1


def test_expired_final_proposal_allows_one_new_attempt(lead_store, monkeypatch):
    FakeAssafOsPort.proposals = []
    FakeAssafOsPort.snapshot_rows = []
    monkeypatch.setattr(assaf_os_leads, "AssafOsPort", FakeAssafOsPort)
    _seed_source(lead_store)
    principal = Principal.owner(source="telegram", actor_id=OWNER)
    first = prepare_selected_crm_lead(
        lead_store,
        settings=_settings(),
        principal=principal,
        contact_id="crm-contact-1",
        conversation_id="site-current",
        path="לימוד",
        summary="",
        next_step="",
    )
    lead_store.get_approval_by_approval_id(first.approval_id).expires_at = (
        "2020-01-01T00:00:00+00:00"
    )
    second = prepare_selected_crm_lead(
        lead_store,
        settings=_settings(),
        principal=principal,
        contact_id="crm-contact-1",
        conversation_id="site-current",
        path="אוטומציה",
        summary="new attempt",
        next_step="",
    )
    assert second.created is True
    assert second.approval_id != first.approval_id
    assert len(FakeAssafOsPort.proposals) == 2


def test_phone_provenance_allows_explicit_local_equivalence_but_rejects_invention():
    from app.services.assaf_os import ProposeRequest

    request = ProposeRequest(
        request_id="12345678-1234-4234-8234-123456789012",
        entity="leads",
        record_id=None,
        expected_revision=0,
        input={
            "name": "דנה",
            "phone": "+972502222222",
            "path": "לימוד",
            "status": "חדש",
            "source_note": "owner",
        },
    )
    base = dict(
        store=None,
        brain=None,
        settings=_settings(),
        embedding_port=None,
        principal=Principal.owner(source="telegram", actor_id=OWNER),
        private_owner_chat=True,
        source_ref="tg:1",
        now=datetime.now(UTC),
    )
    assert _contact_provenance(ToolContext(owner_text="050-222-2222", **base), request)
    assert not _contact_provenance(ToolContext(owner_text="050-222-2223", **base), request)


def test_unified_worker_keeps_legacy_sheet_jobs_without_import_or_projection(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'unified-worker.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)

    class TrapSheets:
        def __getattr__(self, _name):
            raise AssertionError("legacy Sheet transport was touched")

    now = datetime.now(UTC).isoformat()
    with factory() as session:
        session.add_all(
            [
                CrmOutboxRow(
                    id="contacts-job",
                    dedupe_key="contacts-job",
                    aggregate_type="contact",
                    aggregate_id="contact-1",
                    destination="contacts",
                    payload_json="{}",
                    status="pending",
                    created_at=now,
                ),
                CrmOutboxRow(
                    id="telegram-job",
                    dedupe_key="telegram-job",
                    aggregate_type="contact",
                    aggregate_id="contact-1",
                    destination="telegram",
                    payload_json=json.dumps(
                        {"conversation_id": "site-1", "recipient_id": OWNER, "text": "x"}
                    ),
                    status="pending",
                    created_at=now,
                ),
            ]
        )
        session.commit()
    calls = []
    worker = CrmDeliveryWorker(
        session_factory=factory,
        sheets=TrapSheets(),
        telegram_handler=lambda payload, reconcile: calls.append((payload, reconcile))
        or "confirmed",
        allowed_telegram_recipient_ids=frozenset({OWNER}),
        legacy_sheet_projection_enabled=False,
    )
    result = worker.run_once(limit=5)
    with factory() as session:
        assert session.get(CrmOutboxRow, "contacts-job").status == "pending"
        assert session.get(CrmOutboxRow, "telegram-job").status == "confirmed"
    assert result.confirmed == 1
    assert len(calls) == 1
    engine.dispose()


def test_model_timeout_preserves_one_claimed_telegram_ping(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'timeout-ping.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as session:
        payload = _seed_source(LeadStore(session))
        session.commit()

    class TimeoutClient:
        def enabled(self):
            return True

        def complete(self, **_kwargs):
            raise LlmError("deadline exceeded")

    FakeAssafOsPort.proposals = []
    FakeAssafOsPort.snapshot_rows = []
    monkeypatch.setattr(assaf_os_leads, "AssafOsPort", FakeAssafOsPort)
    sends = []
    settings = _settings(telegram_bot_token="test-token")
    handler = telegram_receipt_handler(
        factory,
        settings,
        transport=lambda *args: sends.append(args),
        lead_template_client=TimeoutClient(),
    )
    assert handler(payload, False) == "confirmed"
    assert handler(payload, False) == "confirmed"
    assert len(sends) == 1
    assert "הכנת ליד" in sends[0][1]
    engine.dispose()


def test_oversized_enrichment_falls_back_to_one_legal_original_ping(tmp_path, monkeypatch):
    engine = make_engine(f"sqlite:///{tmp_path / 'bounded-ping.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as session:
        payload = _seed_source(
            LeadStore(session),
            captured_fields={
                "name": "א" * 2_000,
                "phone": "+972502222222",
                "source": "AssafWeb",
            },
            raw_fields={
                "name": "א" * 2_000,
                "phone": "050-222-2222",
                "source": "AssafWeb",
            },
        )
        payload["text"] = "x" * 1_874
        session.get(CrmOutboxRow, "outbox-1").payload_json = json.dumps(
            payload, ensure_ascii=False
        )
        session.commit()
    FakeAssafOsPort.proposals = []
    FakeAssafOsPort.snapshot_rows = []
    monkeypatch.setattr(assaf_os_leads, "AssafOsPort", FakeAssafOsPort)
    sends = []
    handler = telegram_receipt_handler(
        factory,
        _settings(telegram_bot_token="test-token"),
        transport=lambda *args: sends.append(args),
    )
    assert handler(payload, False) == "confirmed"
    assert len(sends) == 1
    assert payload["text"] in sends[0][1]
    assert "בחירת מסלול רק מכינה" in sends[0][1]
    assert plain_text_length(sends[0][1]) <= 4_096
    engine.dispose()
