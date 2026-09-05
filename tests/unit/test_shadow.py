import json
from pathlib import Path

import pytest
from app.api.inbound import process_inbound_texts
from app.core.config import AutomationMode
from app.db.models import AiRunRow, CanonicalEventRow, ShadowDecisionRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.policies import POLICY_VERSION
from app.domain.sales import NextAction
from app.domain.shadow import persist_shadow_decision, should_skip_prospect_send
from app.integrations.base import RecordingMessagePort
from app.integrations.calendar import DisabledCalendarPort
from app.integrations.sheets import DisabledSheetsPort
from sqlalchemy import select

PROSPECT_SHADOW_PHONE = "972509994880"
PROSPECT_AUTO_PHONE = "972509994881"
OWNER_SHADOW_PHONE = "972509994882"
VISITOR_TEXT = "hi"


def _shadow_row_dump(row: ShadowDecisionRow) -> str:
    return json.dumps(
        {
            "run_id": row.run_id,
            "lead_id": row.lead_id,
            "channel": row.channel,
            "next_action": row.next_action,
            "proposed_reply": row.proposed_reply,
            "policy_version": row.policy_version,
        }
    )


def test_should_skip_prospect_send_matrix() -> None:
    assert should_skip_prospect_send(AutomationMode.SHADOW, "prospect") is True
    assert should_skip_prospect_send(AutomationMode.SHADOW, "owner") is False
    assert should_skip_prospect_send(AutomationMode.AUTO_APPROVED, "prospect") is False
    assert should_skip_prospect_send(AutomationMode.HYBRID, "prospect") is False


@pytest.mark.asyncio
async def test_prospect_whatsapp_shadow_skips_send_persists_decision(monkeypatch) -> None:
    monkeypatch.setenv("MIA_AUTOMATION_MODE", "shadow")
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
                    "id": "wamid.shadow.1",
                    "from": PROSPECT_SHADOW_PHONE,
                    "text": VISITOR_TEXT,
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_SHADOW_PHONE
        )
        assert port.sent == []
        ai_row = db.scalars(select(AiRunRow).where(AiRunRow.lead_id == lead_id)).one()
        shadow_row = db.scalars(
            select(ShadowDecisionRow).where(ShadowDecisionRow.run_id == ai_row.run_id)
        ).one()
        assert shadow_row.proposed_reply
        assert shadow_row.policy_version == POLICY_VERSION
        assert shadow_row.next_action == NextAction.UNDERSTAND_WORKFLOW.value
        assert VISITOR_TEXT not in _shadow_row_dump(shadow_row)
        out_row = db.scalars(
            select(CanonicalEventRow).where(
                CanonicalEventRow.provider_event_id == "wamid.shadow.1:out"
            )
        ).one_or_none()
        assert out_row is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_prospect_whatsapp_auto_approved_still_sends() -> None:
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
                    "id": "wamid.shadow.auto.1",
                    "from": PROSPECT_AUTO_PHONE,
                    "text": VISITOR_TEXT,
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        assert len(port.sent) == 1
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_AUTO_PHONE
        )
        shadow_row = db.scalars(
            select(ShadowDecisionRow).where(ShadowDecisionRow.lead_id == lead_id)
        ).one_or_none()
        assert shadow_row is None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_whatsapp_shadow_still_sends_no_shadow_row(monkeypatch) -> None:
    monkeypatch.setenv("MIA_AUTOMATION_MODE", "shadow")
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        shadow_run_ids_before = set(db.scalars(select(ShadowDecisionRow.run_id)).all())
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "wamid.shadow.owner.1",
                    "from": OWNER_SHADOW_PHONE,
                    "text": "daily brief",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_SHADOW_PHONE},
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        assert len(port.sent) == 1
        shadow_run_ids_after = set(db.scalars(select(ShadowDecisionRow.run_id)).all())
        assert shadow_run_ids_after == shadow_run_ids_before
    finally:
        db.close()


def test_persist_shadow_decision_duplicate_run_id_writes_once() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(channel=Channel.WEBSITE, external_id="web_shadow_dup")
        db.commit()
        persist_shadow_decision(
            store,
            run_id="run_shadow_dup_1",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.UNDERSTAND_WORKFLOW.value,
            proposed_reply="first reply",
        )
        persist_shadow_decision(
            store,
            run_id="run_shadow_dup_1",
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
            next_action=NextAction.QUALIFY.value,
            proposed_reply="second reply",
        )
        db.commit()
        row = store.get_shadow_decision("run_shadow_dup_1")
        assert row is not None
        assert row.next_action == NextAction.UNDERSTAND_WORKFLOW.value
        assert row.proposed_reply == "first reply"
    finally:
        db.close()


def test_shadow_module_has_no_forbidden_imports() -> None:
    source = Path("app/domain/shadow.py").read_text(encoding="utf-8")
    assert "MessagePort" not in source
    assert "app.graph" not in source
    assert "select_next_action" not in source


def test_should_skip_prospect_send_ignores_demo_mode(monkeypatch) -> None:
    monkeypatch.setenv("MIA_DEMO_MODE", "true")
    assert should_skip_prospect_send(AutomationMode.SHADOW, "prospect") is True
    assert should_skip_prospect_send(AutomationMode.AUTO_APPROVED, "prospect") is False
