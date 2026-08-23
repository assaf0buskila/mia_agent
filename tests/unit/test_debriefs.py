import importlib
import inspect
import json

import pytest
from app.api.inbound import process_inbound_texts
from app.core.capabilities import CapabilityId, require_alive
from app.db.models import CanonicalEventRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.deals import STAGE_MEETING_OFFERED, apply_deal_policy
from app.domain.debriefs import (
    NEXT_STEP_FOLLOW_UP,
    NEXT_STEP_NONE,
    NEXT_STEP_PROPOSAL,
    OUTCOME_HELD,
    OUTCOME_NO_SHOW,
    OwnerDebriefResult,
    ack_for_debrief_result,
    apply_owner_meeting_debrief,
    parse_debrief_next_step,
    parse_debrief_outcome,
)
from app.domain.events import Channel, EventType
from app.domain.sales import NextAction, SalesState
from app.integrations.base import RecordingMessagePort
from app.integrations.calendar import DisabledCalendarPort
from app.integrations.sheets import DisabledSheetsPort
from sqlalchemy import select

OWNER_PHONE = "972509998201"
OWNER_PHONE_2 = "972509998202"
OWNER_PHONE_3 = "972509998203"
OWNER_PHONE_9 = "972509998209"
LEAD_PHONE = "972509998301"
LEAD_PHONE_4 = "972509998304"
LEAD_PHONE_5 = "972509998305"
LEAD_PHONE_6 = "972509998306"
LEAD_PHONE_7 = "972509998307"
LEAD_PHONE_8 = "972509998308"
LEAD_PHONE_9 = "972509998309"
UNKNOWN_LEAD_ID = "lead_deadbeefdead"
OWNER_PHONE_220 = "972509998220"
OWNER_PHONE_221 = "972509998221"
LEAD_PHONE_320 = "972509998320"
LEAD_PHONE_321 = "972509998321"
LEAD_PHONE_322 = "972509998322"
LEAD_PHONE_323 = "972509998323"
LEAD_PHONE_324 = "972509998324"
LEAD_PHONE_325 = "972509998325"


def _open_lead(store: LeadStore, *, external_id: str) -> str:
    _, lead_id = store.open_channel_lead(
        channel=Channel.WHATSAPP, external_id=external_id
    )
    return lead_id


@pytest.mark.asyncio
async def test_owner_debrief_persist_with_lead_id() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        lead_id = _open_lead(store, external_id=LEAD_PHONE)
        db.commit()
        transcript_body = f"אחרי הפגישה {lead_id} דיברנו על תהליך"
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": "evt.debrief.persist.1",
                "from": OWNER_PHONE,
                "text": transcript_body,
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE},
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        row = store.get_meeting_debrief(lead_id)
        assert row is not None
        assert row.outcome == OUTCOME_HELD
        assert row.next_step == "none"
        assert row.estimated_value == ""
        assert row.notes == ""
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == EventType.MEETING_DEBRIEF.value,
                )
            )
        )
        assert len(events) == 1
        assert events[0].provider_event_id == f"{lead_id}:debrief"
        payload = json.loads(events[0].payload_json)
        assert set(payload.keys()) == {"outcome", "next_step"}
        assert payload == {"outcome": OUTCOME_HELD, "next_step": "none"}
        assert len(port.sent) == 1
        assert "נשמר סיכום פגישה" in port.sent[0].text
        assert "לא עדכנתי שווי עסקה" in port.sent[0].text
        assert transcript_body not in port.sent[0].text
        assert lead_id not in port.sent[0].text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_debrief_missing_lead_id_understanding_check() -> None:
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": "evt.debrief.missing.1",
                "from": OWNER_PHONE_2,
                "text": "אחרי הפגישה דיברנו עם יעל",
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE_2},
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.debrief.missing.1"
        )
        assert task is not None
        assert task.task_type == "meeting_debrief"
        assert len(port.sent) == 1
        assert "מה מזהה הליד" in port.sent[0].text
        assert "יעל" not in port.sent[0].text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_debrief_unknown_lead_id() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": "evt.debrief.unknown.1",
                "from": OWNER_PHONE_3,
                "text": f"אחרי הפגישה {UNKNOWN_LEAD_ID} no show",
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE_3},
        )
        db.commit()
        row = store.get_meeting_debrief(UNKNOWN_LEAD_ID)
        assert row is None
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.provider_event_id
                    == f"{UNKNOWN_LEAD_ID}:debrief",
                )
            )
        )
        assert len(events) == 0
        assert len(port.sent) == 1
        assert "לא מצאתי את הליד" in port.sent[0].text
        assert UNKNOWN_LEAD_ID not in port.sent[0].text
    finally:
        db.close()


def test_apply_owner_meeting_debrief_kill_switch_skips() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _open_lead(store, external_id=LEAD_PHONE_4)
        db.commit()
        result = apply_owner_meeting_debrief(
            store,
            text=f"אחרי הפגישה {lead_id}",
            channel=Channel.WHATSAPP,
            kill_switch=True,
        )
        db.commit()
        assert result.status == "skipped"
        assert store.get_meeting_debrief(lead_id) is None
    finally:
        db.close()


def test_upsert_meeting_debrief_rejects_non_empty_estimated_value() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _open_lead(store, external_id=LEAD_PHONE_5)
        store.upsert_meeting_debrief(
            lead_id=lead_id,
            outcome=OUTCOME_HELD,
            next_step="none",
            estimated_value="5000",
            notes="",
        )
        db.commit()
        assert store.get_meeting_debrief(lead_id) is None
        store.upsert_meeting_debrief(
            lead_id=lead_id,
            outcome=OUTCOME_HELD,
            next_step="none",
            estimated_value="",
            notes="raw transcript",
        )
        db.commit()
        assert store.get_meeting_debrief(lead_id) is None
    finally:
        db.close()


def test_build_meeting_debrief_event_payload_allowlist_only() -> None:
    from app.domain.events import build_meeting_debrief_event

    event = build_meeting_debrief_event(
        provider="whatsapp",
        channel=Channel.WHATSAPP,
        lead_id="lead_abcabcabcabc",
        outcome=OUTCOME_HELD,
    )
    assert set(event.payload.keys()) == {"outcome", "next_step"}
    assert event.payload == {"outcome": OUTCOME_HELD, "next_step": "none"}
    assert event.idempotency_key == "lead_abcabcabcabc:debrief"


def test_debriefs_module_never_imports_message_port() -> None:
    module = importlib.import_module("app.domain.debriefs")
    source = inspect.getsource(module)
    assert "MessagePort" not in source
    assert "FollowUp" not in source
    assert "apply_deal" not in source
    assert "Calendar" not in source


def test_require_alive_meeting_debrief() -> None:
    require_alive(CapabilityId.MEETING_DEBRIEF)


def test_dual_write_row_upserts_canonical_first_write_wins() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _open_lead(store, external_id=LEAD_PHONE_6)
        db.commit()
        first = apply_owner_meeting_debrief(
            store,
            text=f"אחרי הפגישה {lead_id} we met",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert first.status == "persisted"
        second = apply_owner_meeting_debrief(
            store,
            text=f"אחרי הפגישה {lead_id} no show",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert second.status == "persisted"
        row = store.get_meeting_debrief(lead_id)
        assert row is not None
        assert row.outcome == OUTCOME_NO_SHOW
        event = store.get_canonical_event(
            provider=Channel.WHATSAPP.value,
            provider_event_id=f"{lead_id}:debrief",
        )
        assert event is not None
        payload = json.loads(event.payload_json)
        assert payload["outcome"] == OUTCOME_HELD
    finally:
        db.close()


def test_no_show_hebrew_phrase() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _open_lead(store, external_id=LEAD_PHONE_7)
        db.commit()
        result = apply_owner_meeting_debrief(
            store,
            text=f"אחרי הפגישה {lead_id} לא הגיע",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert result.status == "persisted"
        row = store.get_meeting_debrief(lead_id)
        assert row is not None
        assert row.outcome == OUTCOME_NO_SHOW
        assert parse_debrief_outcome("לא הגיע") == OUTCOME_NO_SHOW
    finally:
        db.close()


def test_debrief_does_not_change_deal_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _open_lead(store, external_id=LEAD_PHONE_8)
        sales = SalesState(lead_id=lead_id, willingness_to_meet=True)
        store.save_sales(sales)
        apply_deal_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WHATSAPP,
            action=NextAction.OFFER_MEETING.value,
            kill_switch=False,
        )
        db.commit()
        before = store.get_deal(lead_id)
        assert before is not None
        assert before.stage == STAGE_MEETING_OFFERED
        assert before.expected_value == ""
        assert before.closed_value == ""
        apply_owner_meeting_debrief(
            store,
            text=f"אחרי הפגישה {lead_id} deal worth 50000",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        after = store.get_deal(lead_id)
        assert after is not None
        assert after.stage == before.stage
        assert after.expected_value == ""
        assert after.closed_value == ""
    finally:
        db.close()


@pytest.mark.asyncio
async def test_owner_audio_saves_transcript_and_empty_debrief_notes() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        lead_id = _open_lead(store, external_id=LEAD_PHONE_9)
        db.commit()
        owner_text = f"אחרי הפגישה {lead_id} נפגשנו"
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": "evt.debrief.audio.1",
                "from": OWNER_PHONE_9,
                "text": owner_text,
                "source": "audio",
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE_9},
        )
        db.commit()
        transcript = store.get_transcript(
            provider="whatsapp", provider_event_id="evt.debrief.audio.1"
        )
        assert transcript is not None
        assert transcript.transcript == owner_text
        row = store.get_meeting_debrief(lead_id)
        assert row is not None
        assert row.notes == ""
        assert owner_text not in row.notes
    finally:
        db.close()


def test_ack_for_debrief_result_skipped_returns_none() -> None:
    assert ack_for_debrief_result(OwnerDebriefResult(status="skipped")) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("we met", NEXT_STEP_NONE),
        ("need follow up tomorrow", NEXT_STEP_FOLLOW_UP),
        ("schedule follow-up call", NEXT_STEP_FOLLOW_UP),
        ("send a proposal next week", NEXT_STEP_PROPOSAL),
        ("will send proposal", NEXT_STEP_PROPOSAL),
        ("follow up and send proposal", NEXT_STEP_NONE),
        ("צריך מעקב", NEXT_STEP_FOLLOW_UP),
        ("לשלוח הצעה", NEXT_STEP_PROPOSAL),
    ],
)
def test_parse_debrief_next_step(text: str, expected: str) -> None:
    assert parse_debrief_next_step(text) == expected


def test_apply_debrief_follow_up_persists_without_execute() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _open_lead(store, external_id=LEAD_PHONE_320)
        db.commit()
        result = apply_owner_meeting_debrief(
            store,
            text=f"אחרי הפגישה {lead_id} need follow-up",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert result.status == "persisted"
        row = store.get_meeting_debrief(lead_id)
        assert row is not None
        assert row.next_step == NEXT_STEP_FOLLOW_UP
        assert row.estimated_value == ""
        assert row.notes == ""
        assert store.get_follow_up(lead_id) is None
        event = store.get_canonical_event(
            provider=Channel.WHATSAPP.value,
            provider_event_id=f"{lead_id}:debrief",
        )
        assert event is not None
        payload = json.loads(event.payload_json)
        assert payload["next_step"] == NEXT_STEP_FOLLOW_UP
    finally:
        db.close()


def test_apply_debrief_proposal_no_deal_change() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _open_lead(store, external_id=LEAD_PHONE_321)
        sales = SalesState(lead_id=lead_id, willingness_to_meet=True)
        store.save_sales(sales)
        apply_deal_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WHATSAPP,
            action=NextAction.OFFER_MEETING.value,
            kill_switch=False,
        )
        db.commit()
        before = store.get_deal(lead_id)
        assert before is not None
        assert before.stage == STAGE_MEETING_OFFERED
        result = apply_owner_meeting_debrief(
            store,
            text=f"אחרי הפגישה {lead_id} send a proposal",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert result.status == "persisted"
        row = store.get_meeting_debrief(lead_id)
        assert row is not None
        assert row.next_step == NEXT_STEP_PROPOSAL
        after = store.get_deal(lead_id)
        assert after is not None
        assert after.stage == before.stage
        assert after.expected_value == ""
        assert after.closed_value == ""
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_debrief_follow_up_hebrew() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        lead_id = _open_lead(store, external_id=LEAD_PHONE_322)
        db.commit()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": "evt.debrief.followup.1",
                "from": OWNER_PHONE_220,
                "text": f"אחרי הפגישה {lead_id} צריך מעקב",
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE_220},
        )
        db.commit()
        row = store.get_meeting_debrief(lead_id)
        assert row is not None
        assert row.next_step == NEXT_STEP_FOLLOW_UP
        assert len(port.sent) == 1
        assert "נשמר סיכום פגישה" in port.sent[0].text
        assert "לא עדכנתי שווי עסקה" in port.sent[0].text
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_debrief_proposal_hebrew() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        lead_id = _open_lead(store, external_id=LEAD_PHONE_323)
        db.commit()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{
                "id": "evt.debrief.proposal.1",
                "from": OWNER_PHONE_221,
                "text": f"אחרי הפגישה {lead_id} לשלוח הצעה",
            }],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_PHONE_221},
        )
        db.commit()
        row = store.get_meeting_debrief(lead_id)
        assert row is not None
        assert row.next_step == NEXT_STEP_PROPOSAL
    finally:
        db.close()


def test_upsert_meeting_debrief_rejects_unknown_next_step() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _open_lead(store, external_id=LEAD_PHONE_324)
        store.upsert_meeting_debrief(
            lead_id=lead_id,
            outcome=OUTCOME_HELD,
            next_step="execute",
            estimated_value="",
            notes="",
        )
        db.commit()
        assert store.get_meeting_debrief(lead_id) is None
    finally:
        db.close()


def test_build_meeting_debrief_event_next_step_allowlist() -> None:
    from app.domain.events import build_meeting_debrief_event

    event = build_meeting_debrief_event(
        provider="whatsapp",
        channel=Channel.WHATSAPP,
        lead_id="lead_abcabcabcabc",
        outcome=OUTCOME_HELD,
        next_step=NEXT_STEP_FOLLOW_UP,
    )
    assert event.payload == {"outcome": OUTCOME_HELD, "next_step": NEXT_STEP_FOLLOW_UP}
    coerced = build_meeting_debrief_event(
        provider="whatsapp",
        channel=Channel.WHATSAPP,
        lead_id="lead_abcabcabcabc",
        outcome=OUTCOME_HELD,
        next_step="execute",
    )
    assert coerced.payload == {"outcome": OUTCOME_HELD, "next_step": NEXT_STEP_NONE}


def test_dual_write_next_step_row_upserts_canonical_first_write_wins() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        lead_id = _open_lead(store, external_id=LEAD_PHONE_325)
        db.commit()
        first = apply_owner_meeting_debrief(
            store,
            text=f"אחרי הפגישה {lead_id} we met",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert first.status == "persisted"
        second = apply_owner_meeting_debrief(
            store,
            text=f"אחרי הפגישה {lead_id} need follow-up",
            channel=Channel.WHATSAPP,
            kill_switch=False,
        )
        db.commit()
        assert second.status == "persisted"
        row = store.get_meeting_debrief(lead_id)
        assert row is not None
        assert row.next_step == NEXT_STEP_FOLLOW_UP
        event = store.get_canonical_event(
            provider=Channel.WHATSAPP.value,
            provider_event_id=f"{lead_id}:debrief",
        )
        assert event is not None
        payload = json.loads(event.payload_json)
        assert payload["next_step"] == NEXT_STEP_NONE
    finally:
        db.close()
