import json

import pytest
from app.api.inbound import process_inbound_texts
from app.core.capabilities import CapabilityId, require_alive
from app.db.models import CanonicalEventRow, ChannelIdentityRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import (
    Channel,
    EventType,
    build_attribution_event,
    build_behavior_event,
    build_deal_updated_event,
    build_follow_up_event,
    build_handoff_event,
    build_lead_created_event,
    build_meeting_brief_event,
    build_meeting_offered_event,
    build_message_in_event,
    build_message_out_event,
    build_qualification_updated_event,
    build_tool_result_event,
    persist_tool_outcome,
    sanitize_webhook_channel,
    sanitize_webhook_envelope_kind,
    sheets_mirror_outcome,
    transcription_outcome,
    webhook_envelope_kind,
)
from app.domain.sales import FitLevel, PainLevel, SalesState
from app.domain.tools import ToolOutcome
from app.graph.orchestrator import build_graph
from app.graph.state import empty_state
from app.integrations.base import DisabledMessagePort, RecordingMessagePort
from app.integrations.sheets import FakeSheetsPort
from app.main import app
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import func, select

PROSPECT_PHONE = "972509992001"
OWNER_PHONE = "972509992002"
PROSPECT_PHONE_2 = "972509992021"


def test_build_tool_result_event_allowlist_and_idempotency() -> None:
    outcome = ToolOutcome(
        tool="calendar_find_free_slots",
        status="ok",
        result_count=2,
    )
    event = build_tool_result_event(
        provider="website",
        channel=Channel.WEBSITE,
        inbound_provider_event_id="web_msg_1",
        conversation_id="web_sess_1",
        lead_id="lead_1",
        outcome=outcome,
    )
    assert event.event_type == EventType.TOOL_RESULT
    assert event.event_id == "evt_web_msg_1:tool:calendar_find_free_slots"
    assert event.idempotency_key == "web_msg_1:tool:calendar_find_free_slots"
    assert event.actor_role == "system"
    assert event.payload == {
        "tool": "calendar_find_free_slots",
        "status": "ok",
        "result_count": 2,
    }
    assert event.source == {"provider": "website"}
    serialized = json.dumps(event.payload)
    for forbidden in ("email", "phone", "token", "http", "spend", "name", "@"):
        assert forbidden not in serialized.lower()
    with pytest.raises(ValidationError, match="unknown tool"):
        ToolOutcome(tool="bad_tool", status="ok", result_count=0)
    with pytest.raises(ValidationError, match="unknown tool status"):
        ToolOutcome(tool="sheets_mirror", status="bad", result_count=0)


def test_sheets_mirror_outcome_stamps_latency() -> None:
    ok = sheets_mirror_outcome(2, latency_ms=12)
    assert ok.tool == "sheets_mirror"
    assert ok.status == "ok"
    assert ok.result_count == 2
    assert ok.latency_ms == 12
    denied = sheets_mirror_outcome(0, latency_ms=12)
    assert denied.status == "denied"
    assert denied.result_count == 0
    assert denied.latency_ms == 12
    default = sheets_mirror_outcome(1)
    assert default.latency_ms == 0


def test_voice_transcribe_tool_outcome_payload_only() -> None:
    outcome = ToolOutcome(tool="voice_transcribe", status="ok", result_count=1)
    event = build_tool_result_event(
        provider="whatsapp",
        channel=Channel.WHATSAPP,
        inbound_provider_event_id="wamid.audio.stt",
        conversation_id="972501234567",
        lead_id="lead_stt_1",
        outcome=outcome,
    )
    assert event.event_type == EventType.TOOL_RESULT
    assert event.idempotency_key == "wamid.audio.stt:tool:voice_transcribe"
    assert event.payload == {
        "tool": "voice_transcribe",
        "status": "ok",
        "result_count": 1,
    }
    assert set(event.payload.keys()) == {"tool", "status", "result_count"}
    serialized = json.dumps(event.payload)
    assert "clinic missed calls all day uniquely" not in serialized
    assert "text" not in event.payload
    assert transcription_outcome(transcribed=True) == outcome
    assert transcription_outcome(transcribed=False) == ToolOutcome(
        tool="voice_transcribe", status="empty", result_count=0
    )


def test_build_meeting_offered_event_pairs_and_payload() -> None:
    event = build_meeting_offered_event(
        provider="website",
        channel=Channel.WEBSITE,
        run_id="run_meet_evt_1",
        lead_id="lead_meet_1",
        conversation_id="web_meet_evt_1",
    )
    assert event.event_type == EventType.MEETING_OFFERED
    assert event.event_id == "evt_run_meet_evt_1:meet"
    assert event.idempotency_key == "run_meet_evt_1:meet"
    assert event.actor_role == "system"
    assert event.payload == {"next_action": "offer_meeting"}
    assert event.source == {"provider": "website"}
    assert "email" not in event.payload
    assert "phone" not in event.payload
    assert "text" not in event.payload
    assert "token" not in event.payload


def test_build_meeting_brief_event_pairs_and_payload() -> None:
    payload = {
        "channel": "website",
        "fit": "good",
        "pain_level": 2,
        "workflow_known": True,
        "impact_confirmed": True,
        "reflected": True,
        "hypothesis_offered": True,
        "buying_reality_known": True,
        "authority_known": True,
        "timeline_known": False,
        "metric_known": False,
        "willingness_to_meet": True,
        "owner_required": False,
        "active_objection": None,
        "missing_fields": ["timeline", "metric", "budget"],
        "owner_questions": ["timeline", "email"],
        "next_action": "offer_meeting",
        "email": "secret@example.com",
    }
    event = build_meeting_brief_event(
        provider="website",
        channel=Channel.WEBSITE,
        lead_id="lead_brief_1",
        payload=payload,
    )
    assert event.event_type == EventType.MEETING_BRIEF
    assert event.event_id == "evt_lead_brief_1:brief:offer_meeting"
    assert event.idempotency_key == "lead_brief_1:brief:offer_meeting"
    assert event.actor_role == "system"
    assert event.source == {"provider": "website"}
    assert "email" not in event.payload
    assert "phone" not in event.payload
    assert "text" not in event.payload
    assert event.payload["next_action"] == "offer_meeting"
    assert event.payload["missing_fields"] == ["timeline", "metric"]
    assert event.payload["owner_questions"] == ["timeline"]


def test_build_follow_up_event_pairs_and_payload() -> None:
    pending = build_follow_up_event(
        provider="website",
        channel=Channel.WEBSITE,
        lead_id="lead_fu_1",
        reason="meeting_offered",
        status="pending",
    )
    assert pending.event_type == EventType.FOLLOW_UP
    assert pending.event_id == "evt_lead_fu_1:followup:meeting_offered"
    assert pending.idempotency_key == "lead_fu_1:followup:meeting_offered"
    assert pending.actor_role == "system"
    assert pending.payload == {"status": "pending", "reason": "meeting_offered"}
    assert pending.source == {"provider": "website"}
    cancelled = build_follow_up_event(
        provider="whatsapp",
        channel=Channel.WHATSAPP,
        lead_id="lead_fu_1",
        reason="meeting_offered",
        status="cancelled",
    )
    assert cancelled.idempotency_key == "lead_fu_1:followup:meeting_offered:cancelled"
    assert cancelled.payload == {"status": "cancelled", "reason": "meeting_offered"}
    recovered = build_follow_up_event(
        provider="website",
        channel=Channel.WEBSITE,
        lead_id="lead_fu_1",
        reason="meeting_offered",
        status="recovered",
    )
    assert recovered.idempotency_key == "lead_fu_1:followup:meeting_offered:recovered"
    assert recovered.payload == {"status": "recovered", "reason": "meeting_offered"}
    with pytest.raises(ValueError, match="unknown follow-up reason"):
        build_follow_up_event(
            provider="website",
            channel=Channel.WEBSITE,
            lead_id="lead_fu_1",
            reason="ad_hoc",
            status="pending",
        )


def test_build_deal_updated_event_pairs_and_payload() -> None:
    event = build_deal_updated_event(
        provider="website",
        channel=Channel.WEBSITE,
        lead_id="lead_deal_1",
        stage="meeting_offered",
        source="website",
        attribution_confidence="unknown",
    )
    assert event.event_type == EventType.DEAL_UPDATED
    assert event.idempotency_key == "lead_deal_1:deal:meeting_offered"
    assert event.actor_role == "system"
    assert event.payload == {
        "stage": "meeting_offered",
        "source": "website",
        "attribution_confidence": "unknown",
    }
    assert set(event.payload.keys()) == {"stage", "source", "attribution_confidence"}
    assert "expected_value" not in event.payload
    assert "closed_value" not in event.payload
    with pytest.raises(ValueError, match="unknown deal stage"):
        build_deal_updated_event(
            provider="website",
            channel=Channel.WEBSITE,
            lead_id="lead_deal_1",
            stage="won",
            source="website",
            attribution_confidence="utm",
        )


def test_build_behavior_event_pairs_and_payload() -> None:
    event = build_behavior_event(
        session_id="web_beh_1",
        lead_id="lead_beh_1",
        payload={"kind": "page_viewed", "path": "/he/pricing"},
    )
    assert event.event_type == EventType.BEHAVIOR
    assert event.event_id == "evt_web_beh_1:page:/he/pricing"
    assert event.idempotency_key == "web_beh_1:page:/he/pricing"
    assert event.actor_role == "prospect"
    assert event.payload == {"kind": "page_viewed", "path": "/he/pricing"}
    assert event.source == {"provider": "website"}
    server_event = build_behavior_event(
        session_id="web_beh_1",
        lead_id="lead_beh_1",
        payload={"kind": "mia_opened"},
    )
    assert server_event.actor_role == "system"
    assert server_event.idempotency_key == "web_beh_1:mia_opened"
    dirty = build_behavior_event(
        session_id="web_beh_1",
        lead_id="lead_beh_1",
        payload={"kind": "form_abandoned", "email": "a@b.com", "token": "x"},
    )
    assert dirty.payload == {"kind": "form_abandoned"}
    assert "email" not in dirty.payload
    assert "token" not in dirty.payload
    started = build_behavior_event(
        session_id="web_beh_1",
        lead_id="lead_beh_1",
        payload={"kind": "form_started", "email": "a@b.com"},
    )
    assert started.payload == {"kind": "form_started"}
    assert started.idempotency_key == "web_beh_1:form_started"


def test_build_handoff_event_pairs_and_payload() -> None:
    event = build_handoff_event(
        provider="website",
        channel=Channel.WEBSITE,
        run_id="run_handoff_evt_1",
        lead_id="lead_handoff_1",
        conversation_id="web_handoff_evt_1",
    )
    assert event.event_type == EventType.HANDOFF
    assert event.event_id == "evt_run_handoff_evt_1:handoff"
    assert event.idempotency_key == "run_handoff_evt_1:handoff"
    assert event.actor_role == "system"
    assert event.payload == {"next_action": "handoff"}
    assert event.source == {"provider": "website"}
    assert "email" not in event.payload
    assert "phone" not in event.payload
    assert "text" not in event.payload
    assert "token" not in event.payload


def _ready_to_meet_state(lead_id: str) -> SalesState:
    return SalesState(
        lead_id=lead_id,
        workflow_known=True,
        pain_level=PainLevel.P3,
        impact_confirmed=True,
        reflected=True,
        hypothesis_offered=True,
        authority_known=True,
        timeline_known=True,
        metric_known=True,
        buying_reality_known=True,
        fit=FitLevel.GOOD,
        willingness_to_meet=True,
        missing_fields=[],
    )


def test_graph_persists_meeting_offered() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_meet_evt_1"
        )
        store.save_sales(_ready_to_meet_state(lead_id))
        db.commit()
        run_id = "run_meet_evt_1"
        build_graph(store).invoke(
            empty_state(
                run_id=run_id,
                thread_id="web_meet_evt_1",
                channel="website",
                lead_id=lead_id,
                latest_message="ok",
            )
        )
        db.commit()
        row = store.get_canonical_event(provider="website", provider_event_id=f"{run_id}:meet")
        assert row is not None
        assert row.event_type == "meeting_offered"
        assert row.lead_id == lead_id
        payload = json.loads(row.payload_json)
        assert payload == {"next_action": "offer_meeting"}
        assert "email" not in payload
        assert "phone" not in payload
        meet_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.event_type == "meeting_offered",
                    CanonicalEventRow.lead_id == lead_id,
                )
            )
        )
        assert len(meet_rows) == 1
    finally:
        db.close()


def test_graph_persists_handoff_not_meeting() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_handoff_evt_1"
        )
        store.save_sales(
            SalesState(lead_id=lead_id, owner_required=True, workflow_known=True)
        )
        db.commit()
        run_id = "run_handoff_evt_1"
        build_graph(store).invoke(
            empty_state(
                run_id=run_id,
                thread_id="web_handoff_evt_1",
                channel="website",
                lead_id=lead_id,
                latest_message="ok",
            )
        )
        db.commit()
        handoff_row = store.get_canonical_event(
            provider="website", provider_event_id=f"{run_id}:handoff"
        )
        assert handoff_row is not None
        assert handoff_row.event_type == "handoff"
        assert json.loads(handoff_row.payload_json) == {"next_action": "handoff"}
        meet_row = store.get_canonical_event(
            provider="website", provider_event_id=f"{run_id}:meet"
        )
        assert meet_row is None
        handoff_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.event_type == "handoff",
                    CanonicalEventRow.lead_id == lead_id,
                )
            )
        )
        assert len(handoff_rows) == 1
    finally:
        db.close()


def test_build_lead_created_event_pairs_and_payload() -> None:
    event = build_lead_created_event(
        provider="whatsapp",
        channel=Channel.WHATSAPP,
        lead_id="lead_abc",
        conversation_id="972509992021",
    )
    assert event.event_type == EventType.LEAD_CREATED
    assert event.event_id == "evt_lead_abc:created"
    assert event.idempotency_key == "lead_abc:created"
    assert event.actor_role == "system"
    assert event.lead_id == "lead_abc"
    assert event.conversation_id == "972509992021"
    assert event.payload == {"stage": "open"}
    assert event.source == {"provider": "whatsapp"}
    assert "token" not in event.payload
    assert "secret" not in event.payload
    assert "email" not in event.payload
    assert "phone" not in event.payload


def test_save_canonical_event_stamps_payload_version() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE,
            external_id="web_payload_ver_1",
        )
        event = build_message_in_event(
            provider="website",
            channel=Channel.WEBSITE,
            provider_event_id="evt.payload.stamp.1",
            conversation_id="web_payload_ver_1",
            text="hi",
            actor_role="prospect",
            lead_id=lead_id,
        )
        assert event.payload_version == ""
        store.save_canonical_event(provider="website", event=event)
        db.commit()
        assert event.payload_version == "1"
        row = store.get_canonical_event(
            provider="website", provider_event_id="evt.payload.stamp.1"
        )
        assert row is not None
        assert row.payload_version == "1"
        assert "payload_version" not in json.loads(row.payload_json)
    finally:
        db.close()


def test_build_qualification_updated_event_strips_unknown_keys() -> None:
    event = build_qualification_updated_event(
        provider="whatsapp",
        channel=Channel.WHATSAPP,
        run_id="run_xyz",
        lead_id="lead_abc",
        conversation_id="972509992021",
        payload={
            "fit": "unknown",
            "pain_level": 1,
            "workflow_known": True,
            "text": "secret message",
            "email": "a@b.com",
        },
    )
    assert event.event_type == EventType.QUALIFICATION_UPDATED
    assert event.event_id == "evt_run_xyz:qual"
    assert event.idempotency_key == "run_xyz:qual"
    assert event.actor_role == "system"
    assert "text" not in event.payload
    assert "email" not in event.payload
    assert event.payload["workflow_known"] is True
    assert event.payload["pain_level"] == 1


def test_build_qualification_updated_event_includes_active_objection() -> None:
    event = build_qualification_updated_event(
        provider="website",
        channel=Channel.WEBSITE,
        run_id="run_obj_qual",
        lead_id="lead_obj_qual",
        conversation_id="web_obj_qual",
        payload={
            "fit": "unknown",
            "pain_level": 0,
            "workflow_known": False,
            "active_objection": "price",
            "secret": "drop-me",
        },
    )
    assert event.payload["active_objection"] == "price"
    assert "secret" not in event.payload


def test_build_qualification_updated_event_includes_whatsapp_handoff_offered() -> None:
    event = build_qualification_updated_event(
        provider="website",
        channel=Channel.WEBSITE,
        run_id="run_wa_offer_qual",
        lead_id="lead_wa_offer_qual",
        conversation_id="web_wa_offer_qual",
        payload={
            "fit": "possible",
            "pain_level": 2,
            "workflow_known": True,
            "whatsapp_handoff_offered": True,
            "secret": "drop-me",
        },
    )
    assert event.payload["whatsapp_handoff_offered"] is True
    assert "secret" not in event.payload


def test_build_qualification_updated_event_strips_bad_missing_fields() -> None:
    event = build_qualification_updated_event(
        provider="website",
        channel=Channel.WEBSITE,
        run_id="run_missing_fields",
        lead_id="lead_missing_fields",
        conversation_id="web_missing_fields",
        payload={
            "fit": "good",
            "pain_level": 3,
            "authority_known": True,
            "missing_fields": ["metric", "budget", "champion", "email"],
            "email": "a@b.com",
        },
    )
    assert event.payload["authority_known"] is True
    assert event.payload["missing_fields"] == ["metric"]
    assert "email" not in event.payload
    assert "budget" not in event.payload


def test_build_qualification_updated_event_drops_non_list_missing_fields() -> None:
    event = build_qualification_updated_event(
        provider="website",
        channel=Channel.WEBSITE,
        run_id="run_missing_not_list",
        lead_id="lead_missing_not_list",
        conversation_id="web_missing_not_list",
        payload={"fit": "possible", "missing_fields": "decision_maker"},
    )
    assert "missing_fields" not in event.payload
    assert event.payload["fit"] == "possible"


def test_store_round_trips_meddpicc_flags() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_meddpicc_store"
        )
        sales = SalesState(
            lead_id=lead_id,
            authority_known=True,
            timeline_known=False,
            metric_known=True,
            missing_fields=["timeline"],
        )
        store.save_sales(sales)
        db.commit()
        loaded = store.get_sales(lead_id)
        assert loaded.authority_known is True
        assert loaded.timeline_known is False
        assert loaded.metric_known is True
        assert loaded.missing_fields == ["timeline"]
    finally:
        db.close()
    event = build_attribution_event(
        provider="website",
        channel=Channel.WEBSITE,
        lead_id="lead_attr_1",
        conversation_id="web_attr_1",
        payload={
            "utm_source": "meta",
            "utm_campaign": "yuma",
            "email": "a@b.com",
            "text": "ignored",
            "token": "secret-value",
        },
    )
    assert event.event_type == EventType.ATTRIBUTION
    assert event.event_id == "evt_lead_attr_1:attribution"
    assert event.idempotency_key == "lead_attr_1:attribution"
    assert event.actor_role == "system"
    assert event.payload == {"utm_source": "meta", "utm_campaign": "yuma"}
    assert "email" not in event.payload
    assert "text" not in event.payload
    assert "token" not in event.payload
    assert event.source == {"provider": "website"}


def test_open_channel_lead_persists_lead_created_once() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _c1, l1 = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE_2
        )
        db.commit()
        created = store.get_canonical_event(
            provider="whatsapp", provider_event_id=f"{l1}:created"
        )
        assert created is not None
        assert created.event_type == "lead_created"
        assert json.loads(created.payload_json) == {"stage": "open"}
        _c2, l2 = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE_2
        )
        db.commit()
        assert l1 == l2
        count = db.scalar(
            select(func.count())
            .select_from(CanonicalEventRow)
            .where(CanonicalEventRow.event_type == "lead_created")
            .where(CanonicalEventRow.lead_id == l1)
        )
        assert count == 1
    finally:
        db.close()


def test_build_message_out_event_pairs_and_truncates() -> None:
    text = "x" * 2500
    event = build_message_out_event(
        provider="whatsapp",
        channel=Channel.WHATSAPP,
        inbound_provider_event_id="in.1",
        conversation_id="97250",
        text=text,
        lead_id="lead_abc",
    )
    assert event.event_type == EventType.MESSAGE_OUT
    assert event.event_id == "evt_in.1:out"
    assert event.idempotency_key == "in.1:out"
    assert event.actor_role == "mia"
    assert event.lead_id == "lead_abc"
    assert event.source == {"provider": "whatsapp"}
    assert len(event.payload["text"]) == 2000


@pytest.mark.asyncio
async def test_prospect_inbound_persists_message_in_and_out() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        text = "We run a clinic and miss calls all day."
        event_id = "evt.prospect.out.1"
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": event_id, "from": PROSPECT_PHONE, "text": text}],
            store=store,
            port=port,
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        in_row = store.get_canonical_event(provider="whatsapp", provider_event_id=event_id)
        out_row = store.get_canonical_event(
            provider="whatsapp", provider_event_id=f"{event_id}:out"
        )
        assert in_row is not None
        assert out_row is not None
        assert in_row.event_type == "message_in"
        assert out_row.event_type == "message_out"
        assert in_row.actor_role == "prospect"
        assert out_row.actor_role == "mia"
        assert in_row.lead_id is not None
        assert out_row.lead_id == in_row.lead_id
        in_payload = json.loads(in_row.payload_json)
        out_payload = json.loads(out_row.payload_json)
        assert in_payload["text"] == text
        assert out_payload["text"] == port.sent[0].text
        assert "token" not in in_payload
        assert "secret" not in in_payload
        assert "token" not in out_payload
        assert "secret" not in out_payload
        assert json.loads(in_row.source_json) == {"provider": "whatsapp"}
        assert json.loads(out_row.source_json) == {"provider": "whatsapp"}
        lead_created = store.get_canonical_event(
            provider="whatsapp", provider_event_id=f"{in_row.lead_id}:created"
        )
        assert lead_created is not None
        assert lead_created.event_type == "lead_created"
        qual_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.event_type == "qualification_updated",
                    CanonicalEventRow.lead_id == in_row.lead_id,
                )
            )
        )
        assert len(qual_rows) == 1
        qual_payload = json.loads(qual_rows[0].payload_json)
        assert qual_payload["workflow_known"] is True
        assert "phone" not in qual_payload
        assert "text" not in qual_payload
    finally:
        db.close()


@pytest.mark.asyncio
async def test_prospect_inbound_creates_message_in_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        text = "We run a clinic and miss calls all day."
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": "evt.prospect.1", "from": PROSPECT_PHONE, "text": text}],
            store=store,
            port=port,
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        row = store.get_canonical_event(provider="whatsapp", provider_event_id="evt.prospect.1")
        assert row is not None
        assert row.event_type == "message_in"
        assert row.actor_role == "prospect"
        assert row.lead_id is not None
        payload = json.loads(row.payload_json)
        assert payload["text"] == text
        assert "token" not in payload
        assert "secret" not in payload
        source = json.loads(row.source_json)
        assert source == {"provider": "whatsapp"}
        assert "token" not in source
        assert "secret" not in source
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_send_failure_persists_in_not_out() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        event_id = "evt.fail.1"
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": event_id, "from": PROSPECT_PHONE, "text": "hello"}],
            store=store,
            port=DisabledMessagePort(),
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        in_row = store.get_canonical_event(provider="whatsapp", provider_event_id=event_id)
        out_row = store.get_canonical_event(
            provider="whatsapp", provider_event_id=f"{event_id}:out"
        )
        assert in_row is not None
        assert out_row is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_inbound_persists_message_out_mia_no_lead() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        event_id = "evt.owner.out.1"
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": event_id,
                    "from": OWNER_PHONE,
                    "text": "Schedule a follow-up with Daniel tomorrow.",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE},
            sheets=FakeSheetsPort(),
        )
        db.commit()
        in_row = store.get_canonical_event(provider="whatsapp", provider_event_id=event_id)
        out_row = store.get_canonical_event(
            provider="whatsapp", provider_event_id=f"{event_id}:out"
        )
        assert in_row is not None
        assert out_row is not None
        assert in_row.actor_role == "owner"
        assert out_row.actor_role == "mia"
        assert in_row.lead_id is None
        assert out_row.lead_id is None
        created_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.event_type == "lead_created",
                    CanonicalEventRow.conversation_id == OWNER_PHONE,
                )
            )
        )
        assert len(created_rows) == 0
        assert len(port.sent) == 1
        assert json.loads(out_row.payload_json)["text"] == port.sent[0].text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_inbound_creates_message_in_no_lead() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.1",
                    "from": OWNER_PHONE,
                    "text": "Schedule a follow-up with Daniel tomorrow.",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE},
            sheets=FakeSheetsPort(),
        )
        db.commit()
        row = store.get_canonical_event(provider="whatsapp", provider_event_id="evt.owner.1")
        assert row is not None
        assert row.actor_role == "owner"
        assert row.lead_id is None
        identity = db.scalars(
            select(ChannelIdentityRow).where(ChannelIdentityRow.external_id == OWNER_PHONE)
        ).first()
        assert identity is None
        assert len(port.sent) == 1
        assert "משימת מכירות" in port.sent[0].text
        assert "how the business works" not in port.sent[0].text
        assert "יום רגיל בעסק" not in port.sent[0].text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_duplicate_provider_event_id_one_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        item = {"id": "evt.dup.1", "from": PROSPECT_PHONE, "text": "hello again"}
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[item],
            store=store,
            port=port,
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[item],
            store=store,
            port=port,
            kill_switch=False,
            sheets=FakeSheetsPort(),
        )
        db.commit()
        count = db.scalar(
            select(func.count())
            .select_from(CanonicalEventRow)
            .where(CanonicalEventRow.provider_event_id == "evt.dup.1")
        )
        assert count == 1
        out_count = db.scalar(
            select(func.count())
            .select_from(CanonicalEventRow)
            .where(CanonicalEventRow.provider_event_id == "evt.dup.1:out")
        )
        assert out_count == 1
        lead_id = db.scalar(
            select(CanonicalEventRow.lead_id)
            .where(CanonicalEventRow.provider_event_id == "evt.dup.1")
            .limit(1)
        )
        created_count = db.scalar(
            select(func.count())
            .select_from(CanonicalEventRow)
            .where(CanonicalEventRow.provider_event_id == f"{lead_id}:created")
        )
        assert created_count == 1
    finally:
        db.close()


def test_website_post_message_persists_message_in_and_out() -> None:
    init_db()
    with TestClient(app) as client:
        created = client.post("/v1/website/sessions")
        assert created.status_code == 200
        session_id = created.json()["session_id"]
        reply = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "hi"},
        )
        assert reply.status_code == 200
        body = reply.json()
    db = get_session_factory()()
    try:
        rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.conversation_id == session_id
                )
            )
        )
        in_rows = [row for row in rows if row.event_type == "message_in"]
        out_rows = [row for row in rows if row.event_type == "message_out"]
        created_rows = [row for row in rows if row.event_type == "lead_created"]
        qual_rows = [row for row in rows if row.event_type == "qualification_updated"]
        meet_rows = [row for row in rows if row.event_type == "meeting_offered"]
        tool_rows = [row for row in rows if row.event_type == "tool_result"]
        visitor_in = [
            row
            for row in in_rows
            if json.loads(row.payload_json).get("text") == "hi"
        ]
        assert len(visitor_in) == 1
        assert len(out_rows) == 1
        assert created_rows == []
        assert qual_rows == []
        assert meet_rows == []
        sheets_tools = [
            row
            for row in tool_rows
            if json.loads(row.payload_json).get("tool") == "sheets_mirror"
        ]
        assert sheets_tools == []
        assert visitor_in[0].actor_role == "prospect"
        assert out_rows[0].actor_role == "mia"
        assert visitor_in[0].lead_id in {"", None}
        assert out_rows[0].lead_id in {"", None}
        assert body["lead_id"] == ""
        assert out_rows[0].provider_event_id == f"{visitor_in[0].provider_event_id}:out"
        assert json.loads(visitor_in[0].payload_json) == {"text": "hi"}
        assert json.loads(out_rows[0].payload_json)["text"] == body["message"]
        assert json.loads(visitor_in[0].source_json) == {"provider": "website"}
        assert json.loads(out_rows[0].source_json) == {"provider": "website"}
    finally:
        db.close()


def test_duplicate_tool_result_persist_one_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        outcome = ToolOutcome(tool="sheets_mirror", status="ok", result_count=1)
        for _ in range(2):
            persist_tool_outcome(
                store,
                provider="website",
                channel=Channel.WEBSITE,
                inbound_provider_event_id="dup_in_1",
                conversation_id="web_dup_1",
                lead_id="lead_dup_1",
                outcome=outcome,
            )
        db.commit()
        count = db.scalar(
            select(func.count())
            .select_from(CanonicalEventRow)
            .where(
                CanonicalEventRow.provider_event_id == "dup_in_1:tool:sheets_mirror"
            )
        )
        assert count == 1
    finally:
        db.close()


def test_require_alive_canonical_events_passes_aws_runtime_rejects() -> None:
    require_alive(CapabilityId.CANONICAL_EVENTS)
    with pytest.raises(RuntimeError):
        require_alive(CapabilityId.AWS_RUNTIME)


def test_webhook_envelope_kind_igref_referral_even_with_text() -> None:
    assert webhook_envelope_kind({"id": "igref:envl.sender:ad1", "text": "hello"}) == "referral"


def test_webhook_envelope_kind_audio_source() -> None:
    item = {"id": "envl.audio.1", "source": "audio", "text": "transcript"}
    assert webhook_envelope_kind(item) == "audio"


def test_webhook_envelope_kind_empty_text() -> None:
    assert webhook_envelope_kind({"id": "envl.empty.1", "text": "   "}) == "empty"
    assert webhook_envelope_kind({"id": "envl.empty.2", "text": ""}) == "empty"


def test_webhook_envelope_kind_text() -> None:
    assert webhook_envelope_kind({"id": "envl.text.1", "text": "hi"}) == "text"


def test_webhook_envelope_kind_image() -> None:
    item = {"id": "envl.img.1", "text": "", "photo_file_id": "ph1"}
    assert webhook_envelope_kind(item) == "image"
    assert sanitize_webhook_envelope_kind("image") == "image"


def test_sanitize_webhook_envelope_kind_invalid() -> None:
    assert sanitize_webhook_envelope_kind("bogus") == ""
    assert sanitize_webhook_envelope_kind("text") == "text"


def test_sanitize_webhook_channel_invalid() -> None:
    assert sanitize_webhook_channel("sms") == ""
    assert sanitize_webhook_channel("whatsapp") == "whatsapp"
    assert sanitize_webhook_channel("telegram") == "telegram"
