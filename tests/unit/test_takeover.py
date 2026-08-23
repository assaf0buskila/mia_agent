import inspect
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from app.api.inbound import process_inbound_texts
from app.core.capabilities import CapabilityId, require_alive
from app.core.config import get_settings
from app.core.errors import PolicyDenied
from app.db.models import AiRunRow, CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.conversation_kill import apply_conversation_kill_policy
from app.domain.events import Channel
from app.domain.followups import (
    REASON_MEETING_OFFERED,
    STATUS_PENDING,
    evaluate_follow_up_send,
    follow_up_due_on,
)
from app.domain.owner_tasks import OwnerTaskType, ack_for_owner_task, classify_owner_task
from app.domain.policies.execution_policy import policy_for
from app.domain.sales import FitLevel, NextAction, SalesState
from app.domain.takeover import apply_owner_human_resume, apply_owner_human_takeover
from app.integrations.base import RecordingMessagePort
from app.integrations.calendar import DisabledCalendarPort
from app.integrations.sheets import DisabledSheetsPort
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select

OWNER_TAKEOVER_PHONE = "972509991130"
PROSPECT_TAKEOVER_PHONE = "972509991131"
OWNER_OTHER_PHONE = "972509991132"
OWNER_RESUME_PHONE = "972509991137"
PROSPECT_RESUME_PHONE = "972509991138"
VISITOR_TEXT = "hi"


def _good_sales(lead_id: str) -> SalesState:
    return SalesState(
        lead_id=lead_id,
        fit=FitLevel.GOOD,
        workflow_known=True,
        impact_confirmed=True,
        reflected=True,
        hypothesis_offered=True,
        buying_reality_known=True,
        willingness_to_meet=True,
    )


@pytest.mark.parametrize(
    "text",
    [
        "human takeover lead_abc123456789",
        "take over this lead lead_def012345678",
        "אני לוקח את הליד lead_abc123456789",
        "תפיסה אנושית lead_def012345678",
    ],
)
def test_classify_human_takeover_with_lead_id(text: str) -> None:
    decision = classify_owner_task(text)
    assert decision.task_type == OwnerTaskType.HUMAN_TAKEOVER
    assert decision.needs_clarification is False


@pytest.mark.parametrize(
    "text",
    [
        "human takeover",
        "take over this lead",
        "אני לוקח את הליד",
        "תפיסה אנושית",
    ],
)
def test_classify_human_takeover_without_lead_id_needs_clarification(text: str) -> None:
    decision = classify_owner_task(text)
    assert decision.task_type == OwnerTaskType.HUMAN_TAKEOVER
    assert decision.needs_clarification is True
    ack = ack_for_owner_task(decision)
    assert "תפיסה אנושית" in ack
    assert "מזהה הליד" in ack


@pytest.mark.asyncio
async def test_owner_inbound_sets_human_takeover_not_conversation_killed() -> None:
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
                    "id": "wamid.takeover.open.1",
                    "from": PROSPECT_TAKEOVER_PHONE,
                    "text": VISITOR_TEXT,
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_TAKEOVER_PHONE
        )
        db.commit()
        port.sent.clear()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "wamid.takeover.owner.1",
                    "from": OWNER_TAKEOVER_PHONE,
                    "text": f"human takeover {lead_id}",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_TAKEOVER_PHONE},
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        assert store.is_human_takeover(lead_id) is True
        assert store.is_conversation_killed(lead_id) is False
        assert len(port.sent) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_prospect_whatsapp_after_takeover_skips_send_graph_runs() -> None:
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
                    "id": "wamid.takeover.prospect.1",
                    "from": PROSPECT_TAKEOVER_PHONE,
                    "text": VISITOR_TEXT,
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_TAKEOVER_PHONE
        )
        store.set_human_takeover(lead_id, True)
        db.commit()
        port.sent.clear()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "wamid.takeover.prospect.2",
                    "from": PROSPECT_TAKEOVER_PHONE,
                    "text": "tell me more",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        assert port.sent == []
        ai_rows = db.scalars(
            select(AiRunRow).where(AiRunRow.lead_id == lead_id)
        ).all()
        assert len(ai_rows) >= 2
        out_row = db.scalars(
            select(CanonicalEventRow).where(
                CanonicalEventRow.provider_event_id == "wamid.takeover.prospect.2:out"
            )
        ).one_or_none()
        assert out_row is None
    finally:
        db.close()


def test_nba_stop_sets_conversation_killed_not_human_takeover() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "We run a clinic and miss calls all day."},
        )
        client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "ok that's right"},
        )
        client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "I decide this quarter"},
        )
        client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "let's book a meeting"},
        )
        stop = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "not interested"},
        )
        assert stop.json()["next_action"] == "stop"
        lead_id = stop.json()["lead_id"]
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert store.is_conversation_killed(lead_id) is True
        assert store.is_human_takeover(lead_id) is False
    finally:
        db.close()


def test_apply_conversation_kill_policy_does_not_set_human_takeover() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_takeover_stop"
        )
        apply_conversation_kill_policy(
            store,
            lead_id=lead_id,
            action=NextAction.STOP.value,
        )
        db.commit()
        assert store.is_conversation_killed(lead_id) is True
        assert store.is_human_takeover(lead_id) is False
    finally:
        db.close()


def test_evaluate_follow_up_send_human_takeover() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id="972509991133"
        )
        store.set_human_takeover(lead_id, True)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        store.upsert_follow_up(
            lead_id=lead_id,
            channel=Channel.WHATSAPP.value,
            reason=REASON_MEETING_OFFERED,
            status=STATUS_PENDING,
            due_at=due_at,
        )
        decision = evaluate_follow_up_send(
            store,
            lead_id=lead_id,
            sales=_good_sales(lead_id),
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        assert decision.allowed is False
        assert decision.reason == "human_takeover"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_whatsapp_ack_still_sends_when_other_lead_in_takeover() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, taken_lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id="972509991134"
        )
        store.set_human_takeover(taken_lead_id, True)
        db.commit()
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "wamid.takeover.owner.other.1",
                    "from": OWNER_OTHER_PHONE,
                    "text": "daily brief",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_OTHER_PHONE},
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        assert len(port.sent) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_kill_switch_human_takeover_no_persist() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id="972509991135"
        )
        db.commit()
        port = RecordingMessagePort()
        with pytest.raises(PolicyDenied):
            await process_inbound_texts(
                provider="whatsapp",
                channel=Channel.WHATSAPP,
                items=[
                    {
                        "id": "wamid.takeover.kill.1",
                        "from": OWNER_TAKEOVER_PHONE,
                        "text": f"human takeover {lead_id}",
                    }
                ],
                store=store,
                port=port,
                kill_switch=True,
                owner_ids={OWNER_TAKEOVER_PHONE},
                calendar=DisabledCalendarPort(),
                sheets=DisabledSheetsPort(),
            )
        assert store.is_human_takeover(lead_id) is False
        assert store.is_conversation_killed(lead_id) is False
        assert len(port.sent) == 0
    finally:
        db.close()


def test_apply_owner_human_takeover_kill_switch_format_only() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id="972509991136"
        )
        ack = apply_owner_human_takeover(
            store,
            text=f"human takeover {lead_id}",
            kill_switch=True,
        )
        db.commit()
        assert ack is not None
        assert "תפיסה אנושית" in ack
        assert store.is_human_takeover(lead_id) is False
    finally:
        db.close()


def test_takeover_module_has_no_forbidden_imports() -> None:
    source = Path("app/domain/takeover.py").read_text(encoding="utf-8")
    assert "MessagePort" not in source
    assert "app.graph" not in source
    assert "select_next_action" not in source
    compiled = inspect.getsource(apply_owner_human_takeover)
    assert "MessagePort" not in compiled


def test_fde_human_takeover_capability_alive() -> None:
    require_alive(CapabilityId.FDE_HUMAN_TAKEOVER)
    policy = policy_for(CapabilityId.FDE_HUMAN_TAKEOVER)
    assert policy.capability == CapabilityId.FDE_HUMAN_TAKEOVER.value


@pytest.mark.parametrize(
    "text",
    [
        "resume this lead lead_abc123456789",
        "release this lead lead_def012345678",
        "mia can reply lead_abc123456789",
        "שחרר את הליד lead_def012345678",
        "החזר למיאה lead_abc123456789",
    ],
)
def test_classify_human_takeover_resume_with_lead_id(text: str) -> None:
    decision = classify_owner_task(text)
    assert decision.task_type == OwnerTaskType.HUMAN_TAKEOVER_RESUME
    assert decision.needs_clarification is False


@pytest.mark.parametrize(
    "text",
    [
        "resume this lead",
        "release this lead",
        "mia can reply",
        "שחרר את הליד",
        "החזר למיאה",
    ],
)
def test_classify_human_takeover_resume_without_lead_id_needs_clarification(
    text: str,
) -> None:
    decision = classify_owner_task(text)
    assert decision.task_type == OwnerTaskType.HUMAN_TAKEOVER_RESUME
    assert decision.needs_clarification is True
    ack = ack_for_owner_task(decision)
    assert "שחרור תפיסה" in ack
    assert "מזהה הליד" in ack


def test_resume_this_lead_not_classified_as_sales() -> None:
    decision = classify_owner_task("resume this lead lead_abc123456789")
    assert decision.task_type == OwnerTaskType.HUMAN_TAKEOVER_RESUME
    assert decision.task_type != OwnerTaskType.SALES


@pytest.mark.asyncio
async def test_owner_inbound_resume_clears_human_takeover() -> None:
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
                    "id": "wamid.resume.prospect.1",
                    "from": PROSPECT_RESUME_PHONE,
                    "text": VISITOR_TEXT,
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_RESUME_PHONE
        )
        store.set_human_takeover(lead_id, True)
        db.commit()
        port.sent.clear()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "wamid.resume.owner.1",
                    "from": OWNER_RESUME_PHONE,
                    "text": f"resume this lead {lead_id}",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_RESUME_PHONE},
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        assert store.is_human_takeover(lead_id) is False
        assert store.is_conversation_killed(lead_id) is False
        assert len(port.sent) == 1
    finally:
        db.close()


@pytest.mark.asyncio
async def test_prospect_whatsapp_after_resume_sends_again() -> None:
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
                    "id": "wamid.resume.prospect.open.1",
                    "from": PROSPECT_RESUME_PHONE,
                    "text": VISITOR_TEXT,
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_RESUME_PHONE
        )
        store.set_human_takeover(lead_id, True)
        db.commit()
        port.sent.clear()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "wamid.resume.owner.clear.1",
                    "from": OWNER_RESUME_PHONE,
                    "text": f"release this lead {lead_id}",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_RESUME_PHONE},
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        assert store.is_human_takeover(lead_id) is False
        port.sent.clear()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "wamid.resume.prospect.2",
                    "from": PROSPECT_RESUME_PHONE,
                    "text": "tell me more",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        assert len(port.sent) >= 1
    finally:
        db.close()


def test_apply_owner_human_resume_kill_switch_format_only() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_RESUME_PHONE
        )
        store.set_human_takeover(lead_id, True)
        ack = apply_owner_human_resume(
            store,
            text=f"resume this lead {lead_id}",
            kill_switch=True,
        )
        db.commit()
        assert ack is not None
        assert "שוחררה" in ack
        assert store.is_human_takeover(lead_id) is True
    finally:
        db.close()


def test_apply_owner_human_resume_unknown_lead() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        ack = apply_owner_human_resume(
            store,
            text="resume this lead lead_000000000001",
            kill_switch=False,
        )
        assert ack is not None
        assert "שחרור תפיסה" in ack
        assert "לא מצאתי את הליד" in ack
    finally:
        db.close()


def test_evaluate_follow_up_send_after_resume_not_blocked_by_takeover() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_RESUME_PHONE
        )
        store.set_human_takeover(lead_id, True)
        apply_owner_human_resume(
            store,
            text=f"mia can reply {lead_id}",
            kill_switch=False,
        )
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        store.upsert_follow_up(
            lead_id=lead_id,
            channel=Channel.WHATSAPP.value,
            reason=REASON_MEETING_OFFERED,
            status=STATUS_PENDING,
            due_at=due_at,
        )
        decision = evaluate_follow_up_send(
            store,
            lead_id=lead_id,
            sales=_good_sales(lead_id),
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        assert decision.reason != "human_takeover"
    finally:
        db.close()
