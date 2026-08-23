import inspect
import json
import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from app.api.inbound import process_inbound_texts
from app.core.config import get_settings
from app.db.models import CanonicalEventRow, FollowUpRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel, build_behavior_event, build_message_out_event
from app.domain.followup_voice import (
    MEETING_OFFERED_FOLLOW_UP,
    compose_follow_up_draft,
)
from app.domain.followups import (
    FOLLOW_UP_SCOPE,
    REASON_MEETING_OFFERED,
    STATUS_CANCELLED,
    STATUS_PENDING,
    STATUS_RECOVERED,
    apply_follow_up_policy,
    claim_follow_up_persist,
    complete_follow_up_persist,
    evaluate_follow_up_send,
    follow_up_claim_key,
    follow_up_due_on,
    scan_due_follow_ups,
)
from app.domain.humanity import lint_customer_reply
from app.domain.sales import FitLevel, NextAction, SalesState
from app.integrations.base import RecordingMessagePort
from app.integrations.calendar import DisabledCalendarPort
from app.integrations.sheets import DisabledSheetsPort
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select

PROSPECT_PHONE = "972509994001"
PROSPECT_PHONE_2 = "972509994002"
PROSPECT_PHONE_STOP = "972509994003"
SCAN_PHONE_WA = "972509994201"
SCAN_PHONE_WEB = "972509994202"
SCAN_PHONE_KILL = "972509994203"
SCAN_PHONE_TOMORROW = "972509994204"
SCAN_PHONE_KILLED = "972509994205"
SCAN_PHONE_NO_PORT = "972509994206"
SCAN_PHONE_RESET = "972509994207"
SCAN_PHONE_TZ = "972509994208"
CAP_PHONE_NONE = "972509994211"
CAP_PHONE_CAPPED = "972509994212"
CAP_PHONE_OTHER = "972509994213"
CAP_PHONE_OTHER_TARGET = "972509994214"
CAP_PHONE_YDAY = "972509994215"
CAP_PHONE_SCAN = "972509994216"
CLAIM_PHONE = "972509994221"


def _due_pattern() -> re.Pattern[str]:
    return re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _run_clinic_funnel_to_meeting(client: TestClient, session_id: str) -> str:
    messages = [
        "We run a clinic and miss calls all day.",
        "ok that's right",
        "I decide this quarter",
        "let's book a meeting",
    ]
    lead_id = ""
    for text in messages:
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": text},
        )
        assert response.status_code == 200
        body = response.json()
        lead_id = body["lead_id"]
    assert body["next_action"] == "offer_meeting"
    return lead_id


def test_follow_up_due_on_tomorrow_jerusalem() -> None:
    now = datetime(2026, 8, 21, 20, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
    due_at = follow_up_due_on(now=now, timezone="Asia/Jerusalem")
    assert due_at == "2026-08-22"


def test_website_clinic_funnel_creates_pending_follow_up() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        lead_id = _run_clinic_funnel_to_meeting(client, session_id)
    db = get_session_factory()()
    try:
        row = db.scalars(
            select(FollowUpRow).where(FollowUpRow.lead_id == lead_id)
        ).one()
        assert row.status == STATUS_PENDING
        assert row.reason == REASON_MEETING_OFFERED
        assert row.channel == Channel.WEBSITE.value
        assert _due_pattern().match(row.due_at)
        follow_rows = list(
            db.scalars(select(FollowUpRow).where(FollowUpRow.lead_id == lead_id)).all()
        )
        assert len(follow_rows) == 1
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == "follow_up",
                )
            )
        )
        assert len(events) == 1
        payload = json.loads(events[0].payload_json)
        assert payload == {"status": STATUS_PENDING, "reason": REASON_MEETING_OFFERED}
        assert set(payload.keys()) == {"status", "reason"}
        serialized = json.dumps(events[0].payload_json) + json.dumps(events[0].source_json)
        for forbidden in ("@", "email", "phone", "transcript", "clinic"):
            assert forbidden not in serialized.lower()
    finally:
        db.close()


def test_second_offer_meeting_still_one_pending_row() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        lead_id = _run_clinic_funnel_to_meeting(client, session_id)
        again = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "let's book a meeting"},
        )
        assert again.json()["next_action"] == "offer_meeting"
    db = get_session_factory()()
    try:
        rows = list(db.scalars(select(FollowUpRow).where(FollowUpRow.lead_id == lead_id)))
        assert len(rows) == 1
        assert rows[0].status == STATUS_PENDING
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == "follow_up",
                )
            )
        )
        assert len(events) == 1
    finally:
        db.close()


def test_stop_cancels_pending_follow_up() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        lead_id = _run_clinic_funnel_to_meeting(client, session_id)
        stop = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "not interested"},
        )
        assert stop.json()["next_action"] == "stop"
    db = get_session_factory()()
    try:
        row = db.scalars(
            select(FollowUpRow).where(FollowUpRow.lead_id == lead_id)
        ).one()
        assert row.status == STATUS_CANCELLED
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == "follow_up",
                )
            )
        )
        assert len(events) == 2
        statuses = {json.loads(event.payload_json)["status"] for event in events}
        assert statuses == {STATUS_PENDING, STATUS_CANCELLED}
    finally:
        db.close()


def test_student_disqualify_no_follow_up_row() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "I'm a student with a school project"},
        )
        assert response.status_code == 200
        assert response.json()["next_action"] == "disqualify"
        lead_id = response.json()["lead_id"]
    db = get_session_factory()()
    try:
        row = db.scalars(
            select(FollowUpRow).where(FollowUpRow.lead_id == lead_id)
        ).one_or_none()
        assert row is None
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == "follow_up",
                )
            )
        )
        assert len(events) == 0
    finally:
        db.close()


def test_kill_switch_skips_follow_up_create(monkeypatch) -> None:
    monkeypatch.setenv("MIA_KILL_SWITCH", "true")
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_kill_fu_1"
        )
        sales = SalesState(
            lead_id=lead_id,
            fit=FitLevel.GOOD,
            workflow_known=True,
            impact_confirmed=True,
            reflected=True,
            hypothesis_offered=True,
            buying_reality_known=True,
            willingness_to_meet=True,
        )
        store.save_sales(sales)
        db.commit()
        apply_follow_up_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            timezone=get_settings().calendar_timezone,
            kill_switch=True,
        )
        db.commit()
        assert store.get_follow_up(lead_id) is None
    finally:
        db.close()


def test_later_conversation_recovers_pending_follow_up() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_fu_recovered"
        )
        sales = SalesState(
            lead_id=lead_id,
            fit=FitLevel.GOOD,
            workflow_known=True,
            impact_confirmed=True,
            reflected=True,
            hypothesis_offered=True,
            buying_reality_known=True,
            willingness_to_meet=True,
        )
        store.save_sales(sales)
        settings = get_settings()
        apply_follow_up_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            timezone=settings.calendar_timezone,
            kill_switch=False,
        )
        apply_follow_up_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.HANDLE_OBJECTION.value,
            sales=sales,
            timezone=settings.calendar_timezone,
            kill_switch=False,
        )
        db.commit()
        row = store.get_follow_up(lead_id)
        assert row is not None
        assert row.status == STATUS_RECOVERED
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == "follow_up",
                )
            )
        )
        statuses = {json.loads(event.payload_json)["status"] for event in events}
        assert statuses == {STATUS_PENDING, STATUS_RECOVERED}
    finally:
        db.close()


def test_kill_switch_still_cancels_pending_follow_up() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_kill_cancel_fu"
        )
        sales = SalesState(
            lead_id=lead_id,
            fit=FitLevel.GOOD,
            workflow_known=True,
            impact_confirmed=True,
            reflected=True,
            hypothesis_offered=True,
            buying_reality_known=True,
            willingness_to_meet=True,
        )
        store.save_sales(sales)
        settings = get_settings()
        apply_follow_up_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            timezone=settings.calendar_timezone,
            kill_switch=False,
        )
        apply_follow_up_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.STOP.value,
            sales=sales,
            timezone=settings.calendar_timezone,
            kill_switch=True,
        )
        db.commit()
        row = store.get_follow_up(lead_id)
        assert row is not None
        assert row.status == STATUS_CANCELLED
    finally:
        db.close()


def test_reactivate_cancelled_follow_up() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_reactivate_1"
        )
        sales = SalesState(
            lead_id=lead_id,
            fit=FitLevel.GOOD,
            workflow_known=True,
            impact_confirmed=True,
            reflected=True,
            hypothesis_offered=True,
            buying_reality_known=True,
            willingness_to_meet=True,
        )
        store.save_sales(sales)
        settings = get_settings()
        apply_follow_up_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            timezone=settings.calendar_timezone,
            kill_switch=False,
        )
        apply_follow_up_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.STOP.value,
            sales=sales,
            timezone=settings.calendar_timezone,
            kill_switch=False,
        )
        apply_follow_up_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            timezone=settings.calendar_timezone,
            kill_switch=False,
        )
        db.commit()
        row = store.get_follow_up(lead_id)
        assert row is not None
        assert row.status == STATUS_PENDING
        rows = list(
            db.scalars(select(FollowUpRow).where(FollowUpRow.lead_id == lead_id)).all()
        )
        assert len(rows) == 1
        events = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == "follow_up",
                )
            )
        )
        assert len(events) == 2
    finally:
        db.close()


def test_apply_follow_up_policy_never_calls_message_port() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE
        )
        sales = SalesState(
            lead_id=lead_id,
            fit=FitLevel.GOOD,
            workflow_known=True,
            impact_confirmed=True,
            reflected=True,
            hypothesis_offered=True,
            buying_reality_known=True,
            willingness_to_meet=True,
        )
        store.save_sales(sales)
        port = RecordingMessagePort()
        apply_follow_up_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WHATSAPP,
            action=NextAction.OFFER_MEETING.value,
            sales=sales,
            timezone=get_settings().calendar_timezone,
            kill_switch=False,
        )
        db.commit()
        assert port.sent == []
        assert store.get_follow_up(lead_id) is not None
    finally:
        db.close()


def test_two_leads_each_get_follow_up_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        lead_ids: list[str] = []
        for external_id in ("web_fu_a", "web_fu_b"):
            _, lead_id = store.open_channel_lead(
                channel=Channel.WEBSITE, external_id=external_id
            )
            sales = SalesState(
                lead_id=lead_id,
                fit=FitLevel.GOOD,
                workflow_known=True,
                impact_confirmed=True,
                reflected=True,
                hypothesis_offered=True,
                buying_reality_known=True,
                willingness_to_meet=True,
            )
            store.save_sales(sales)
            apply_follow_up_policy(
                store,
                lead_id=lead_id,
                channel=Channel.WEBSITE,
                action=NextAction.OFFER_MEETING.value,
                sales=sales,
                timezone=settings.calendar_timezone,
                kill_switch=False,
            )
            lead_ids.append(lead_id)
        db.commit()
        rows = list(
            db.scalars(select(FollowUpRow).where(FollowUpRow.lead_id.in_(lead_ids))).all()
        )
        assert len(rows) == 2
        assert {row.lead_id for row in rows} == set(lead_ids)
    finally:
        db.close()


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


def _seed_due_follow_up(
    store: LeadStore,
    *,
    channel: Channel,
    external_id: str,
    due_at: str,
    fit: FitLevel = FitLevel.POSSIBLE,
) -> str:
    _, lead_id = store.open_channel_lead(channel=channel, external_id=external_id)
    store.save_sales(SalesState(lead_id=lead_id, fit=fit))
    store.upsert_follow_up(
        lead_id=lead_id,
        channel=channel.value,
        reason=REASON_MEETING_OFFERED,
        status=STATUS_PENDING,
        due_at=due_at,
    )
    return lead_id


def _follow_up_for_lead(db, lead_id: str) -> FollowUpRow:
    return db.scalars(
        select(FollowUpRow).where(FollowUpRow.lead_id == lead_id)
    ).one()


def test_scan_due_follow_ups_whatsapp_possible_fit_send_ready() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        lead_id = _seed_due_follow_up(
            store,
            channel=Channel.WHATSAPP,
            external_id=SCAN_PHONE_WA,
            due_at=due_at,
            fit=FitLevel.POSSIBLE,
        )
        db.commit()
        results = scan_due_follow_ups(
            store,
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        matching = [item for item in results if item.lead_id == lead_id]
        assert len(matching) == 1
        assert matching[0].allowed is True
        assert matching[0].reason == "due_pending"
        row = _follow_up_for_lead(db, lead_id)
        assert row.send_ready is True
        assert row.block_reason == "due_pending"
        assert row.draft == MEETING_OFFERED_FOLLOW_UP
    finally:
        db.close()


def test_scan_due_follow_ups_website_channel_not_sendable() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        lead_id = _seed_due_follow_up(
            store,
            channel=Channel.WEBSITE,
            external_id=SCAN_PHONE_WEB,
            due_at=due_at,
        )
        db.commit()
        results = scan_due_follow_ups(
            store,
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        matching = [item for item in results if item.lead_id == lead_id]
        assert len(matching) == 1
        assert matching[0].allowed is False
        assert matching[0].reason == "channel_not_sendable"
        row = _follow_up_for_lead(db, lead_id)
        assert row.send_ready is False
        assert row.block_reason == "channel_not_sendable"
        assert row.draft == ""
    finally:
        db.close()


def test_scan_due_follow_ups_kill_switch_persists_scan_fields() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        lead_id = _seed_due_follow_up(
            store,
            channel=Channel.WHATSAPP,
            external_id=SCAN_PHONE_KILL,
            due_at=due_at,
        )
        db.commit()
        results = scan_due_follow_ups(
            store,
            timezone=settings.calendar_timezone,
            kill_switch=True,
            now=now,
        )
        matching = [item for item in results if item.lead_id == lead_id]
        assert len(matching) == 1
        assert matching[0].allowed is False
        assert matching[0].reason == "kill_switch"
        row = _follow_up_for_lead(db, lead_id)
        assert row.send_ready is False
        assert row.block_reason == "kill_switch"
        assert row.draft == ""
    finally:
        db.close()


def test_scan_due_follow_ups_tomorrow_not_scanned() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=1)
        lead_id = _seed_due_follow_up(
            store,
            channel=Channel.WHATSAPP,
            external_id=SCAN_PHONE_TOMORROW,
            due_at=due_at,
        )
        db.commit()
        results = scan_due_follow_ups(
            store,
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        matching = [item for item in results if item.lead_id == lead_id]
        assert matching == []
        row = _follow_up_for_lead(db, lead_id)
        assert row.send_ready is False
        assert row.block_reason == ""
        assert row.draft == ""
    finally:
        db.close()


def test_scan_due_follow_ups_conversation_killed() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        lead_id = _seed_due_follow_up(
            store,
            channel=Channel.WHATSAPP,
            external_id=SCAN_PHONE_KILLED,
            due_at=due_at,
        )
        store.set_conversation_killed(lead_id, True)
        db.commit()
        results = scan_due_follow_ups(
            store,
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        matching = [item for item in results if item.lead_id == lead_id]
        assert len(matching) == 1
        assert matching[0].allowed is False
        assert matching[0].reason == "conversation_killed"
        row = _follow_up_for_lead(db, lead_id)
        assert row.send_ready is False
        assert row.block_reason == "conversation_killed"
        assert row.draft == ""
    finally:
        db.close()


def test_scan_due_follow_ups_never_calls_message_port() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        lead_id = _seed_due_follow_up(
            store,
            channel=Channel.WHATSAPP,
            external_id=SCAN_PHONE_NO_PORT,
            due_at=due_at,
        )
        db.commit()
        port = RecordingMessagePort()
        results = scan_due_follow_ups(
            store,
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        assert any(item.lead_id == lead_id for item in results)
        assert port.sent == []
    finally:
        db.close()


def test_upsert_follow_up_cancel_resets_send_ready() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        lead_id = _seed_due_follow_up(
            store,
            channel=Channel.WHATSAPP,
            external_id=SCAN_PHONE_RESET,
            due_at=due_at,
        )
        db.commit()
        scan_due_follow_ups(
            store,
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        db.commit()
        row = _follow_up_for_lead(db, lead_id)
        assert row.send_ready is True
        assert row.block_reason == "due_pending"
        assert row.draft == MEETING_OFFERED_FOLLOW_UP
        store.upsert_follow_up(
            lead_id=lead_id,
            channel=row.channel,
            reason=row.reason,
            status=STATUS_CANCELLED,
            due_at=row.due_at,
        )
        db.commit()
        row = _follow_up_for_lead(db, lead_id)
        assert row.status == STATUS_CANCELLED
        assert row.send_ready is False
        assert row.block_reason == ""
        assert row.draft == ""
    finally:
        db.close()


def test_scan_due_follow_ups_invalid_timezone_returns_empty() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        lead_id = _seed_due_follow_up(
            store,
            channel=Channel.WHATSAPP,
            external_id=SCAN_PHONE_TZ,
            due_at=due_at,
        )
        db.commit()
        results = scan_due_follow_ups(
            store,
            timezone="Not/A_Real_Zone",
            kill_switch=False,
            now=now,
        )
        assert results == []
        row = _follow_up_for_lead(db, lead_id)
        assert row.send_ready is False
        assert row.block_reason == ""
        assert row.draft == ""
    finally:
        db.close()


def test_evaluate_follow_up_send_whatsapp_due_today_allowed() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE
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
        assert decision.allowed is True
        assert decision.reason == "due_pending"
    finally:
        db.close()


def test_evaluate_follow_up_send_website_channel_not_sendable() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_send_eval_1"
        )
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        store.upsert_follow_up(
            lead_id=lead_id,
            channel=Channel.WEBSITE.value,
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
        assert decision.reason == "channel_not_sendable"
    finally:
        db.close()


def test_evaluate_follow_up_send_instagram_not_due_tomorrow() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.INSTAGRAM, external_id="ig_send_eval_1"
        )
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=1)
        store.upsert_follow_up(
            lead_id=lead_id,
            channel=Channel.INSTAGRAM.value,
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
        assert decision.reason == "not_due"
    finally:
        db.close()


def test_evaluate_follow_up_send_cancelled() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE
        )
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        store.upsert_follow_up(
            lead_id=lead_id,
            channel=Channel.WHATSAPP.value,
            reason=REASON_MEETING_OFFERED,
            status=STATUS_CANCELLED,
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
        assert decision.reason == "cancelled"
    finally:
        db.close()


def test_evaluate_follow_up_send_recovered() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE
        )
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        store.upsert_follow_up(
            lead_id=lead_id,
            channel=Channel.WHATSAPP.value,
            reason=REASON_MEETING_OFFERED,
            status=STATUS_RECOVERED,
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
        assert decision.reason == "recovered"
    finally:
        db.close()


def test_evaluate_follow_up_send_no_row() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id="972509994099"
        )
        settings = get_settings()
        decision = evaluate_follow_up_send(
            store,
            lead_id=lead_id,
            sales=_good_sales(lead_id),
            timezone=settings.calendar_timezone,
            kill_switch=False,
        )
        assert decision.allowed is False
        assert decision.reason == "no_row"
    finally:
        db.close()


def test_evaluate_follow_up_send_kill_switch() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE
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
            kill_switch=True,
            now=now,
        )
        assert decision.allowed is False
        assert decision.reason == "kill_switch"
    finally:
        db.close()


def test_evaluate_follow_up_send_poor_fit() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE
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
        sales = SalesState(lead_id=lead_id, fit=FitLevel.POOR)
        decision = evaluate_follow_up_send(
            store,
            lead_id=lead_id,
            sales=sales,
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        assert decision.allowed is False
        assert decision.reason == "poor_fit"
    finally:
        db.close()


def test_evaluate_follow_up_send_never_touches_message_port() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE
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
        port = RecordingMessagePort()
        evaluate_follow_up_send(
            store,
            lead_id=lead_id,
            sales=_good_sales(lead_id),
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        assert port.sent == []
    finally:
        db.close()


def _seed_message_out(
    store: LeadStore,
    *,
    lead_id: str,
    external_id: str,
    inbound_id: str,
    occurred_at: datetime,
) -> None:
    store.save_canonical_event(
        provider=Channel.WHATSAPP.value,
        event=build_message_out_event(
            provider=Channel.WHATSAPP.value,
            channel=Channel.WHATSAPP,
            inbound_provider_event_id=inbound_id,
            conversation_id=external_id,
            text="reply",
            lead_id=lead_id,
            occurred_at=occurred_at,
        ),
    )


def test_evaluate_follow_up_send_due_pending_without_message_out() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        lead_id = _seed_due_follow_up(
            store,
            channel=Channel.WHATSAPP,
            external_id=CAP_PHONE_NONE,
            due_at=due_at,
        )
        db.commit()
        decision = evaluate_follow_up_send(
            store,
            lead_id=lead_id,
            sales=_good_sales(lead_id),
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        assert decision.allowed is True
        assert decision.reason == "due_pending"
    finally:
        db.close()


def test_evaluate_follow_up_send_frequency_capped_same_local_day() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        lead_id = _seed_due_follow_up(
            store,
            channel=Channel.WHATSAPP,
            external_id=CAP_PHONE_CAPPED,
            due_at=due_at,
        )
        _seed_message_out(
            store,
            lead_id=lead_id,
            external_id=CAP_PHONE_CAPPED,
            inbound_id="wamid.cap.same.1",
            occurred_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
        )
        db.commit()
        decision = evaluate_follow_up_send(
            store,
            lead_id=lead_id,
            sales=_good_sales(lead_id),
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        assert decision.allowed is False
        assert decision.reason == "frequency_capped"
    finally:
        db.close()


def test_evaluate_follow_up_send_other_lead_message_out_does_not_cap() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        lead_id = _seed_due_follow_up(
            store,
            channel=Channel.WHATSAPP,
            external_id=CAP_PHONE_OTHER_TARGET,
            due_at=due_at,
        )
        _, other_lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=CAP_PHONE_OTHER
        )
        _seed_message_out(
            store,
            lead_id=other_lead_id,
            external_id=CAP_PHONE_OTHER,
            inbound_id="wamid.cap.other.1",
            occurred_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
        )
        db.commit()
        decision = evaluate_follow_up_send(
            store,
            lead_id=lead_id,
            sales=_good_sales(lead_id),
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        assert decision.allowed is True
        assert decision.reason == "due_pending"
        capped_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == "message_out",
                )
            ).all()
        )
        assert capped_rows == []
    finally:
        db.close()


def test_evaluate_follow_up_send_yesterday_message_out_does_not_cap() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        lead_id = _seed_due_follow_up(
            store,
            channel=Channel.WHATSAPP,
            external_id=CAP_PHONE_YDAY,
            due_at=due_at,
        )
        _seed_message_out(
            store,
            lead_id=lead_id,
            external_id=CAP_PHONE_YDAY,
            inbound_id="wamid.cap.yesterday.1",
            occurred_at=datetime(2026, 8, 20, 9, 0, tzinfo=UTC),
        )
        db.commit()
        decision = evaluate_follow_up_send(
            store,
            lead_id=lead_id,
            sales=_good_sales(lead_id),
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        assert decision.allowed is True
        assert decision.reason == "due_pending"
    finally:
        db.close()


def test_count_canonical_events_for_lead_rejects_non_frequency_type() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_cap_beh_1"
        )
        store.save_canonical_event(
            provider=Channel.WEBSITE.value,
            event=build_behavior_event(
                session_id="web_cap_beh_1",
                lead_id=lead_id,
                payload={"kind": "mia_opened"},
                occurred_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
            ),
        )
        db.commit()
        bounds_from = datetime(2026, 8, 20, 21, 0, tzinfo=UTC).isoformat()
        bounds_to = datetime(2026, 8, 21, 21, 0, tzinfo=UTC).isoformat()
        count = store.count_canonical_events_for_lead(
            lead_id=lead_id,
            event_type="behavior",
            occurred_from=bounds_from,
            occurred_to=bounds_to,
        )
        assert count == 0
        behavior_rows = list(
            db.scalars(
                select(CanonicalEventRow).where(
                    CanonicalEventRow.lead_id == lead_id,
                    CanonicalEventRow.event_type == "behavior",
                )
            ).all()
        )
        assert len(behavior_rows) == 1
    finally:
        db.close()


def test_scan_due_follow_ups_frequency_capped_persists_scan_fields() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = get_settings()
        now = datetime(2026, 8, 21, 12, 0, tzinfo=ZoneInfo("Asia/Jerusalem"))
        due_at = follow_up_due_on(now=now, timezone=settings.calendar_timezone, offset_days=0)
        lead_id = _seed_due_follow_up(
            store,
            channel=Channel.WHATSAPP,
            external_id=CAP_PHONE_SCAN,
            due_at=due_at,
        )
        _seed_message_out(
            store,
            lead_id=lead_id,
            external_id=CAP_PHONE_SCAN,
            inbound_id="wamid.cap.scan.1",
            occurred_at=datetime(2026, 8, 21, 9, 0, tzinfo=UTC),
        )
        db.commit()
        results = scan_due_follow_ups(
            store,
            timezone=settings.calendar_timezone,
            kill_switch=False,
            now=now,
        )
        matching = [item for item in results if item.lead_id == lead_id]
        assert len(matching) == 1
        assert matching[0].allowed is False
        assert matching[0].reason == "frequency_capped"
        row = _follow_up_for_lead(db, lead_id)
        assert row.send_ready is False
        assert row.block_reason == "frequency_capped"
        assert row.draft == ""
    finally:
        db.close()


def test_compose_follow_up_draft_passes_humanity_lint() -> None:
    draft = compose_follow_up_draft(reason=REASON_MEETING_OFFERED)
    assert draft == MEETING_OFFERED_FOLLOW_UP
    assert lint_customer_reply(draft).ok is True


def test_compose_follow_up_draft_unknown_reason_empty() -> None:
    assert compose_follow_up_draft(reason="meeting_booked") == ""


def test_followup_voice_module_has_no_http_or_ports() -> None:
    import app.domain.followup_voice as followup_voice_mod

    source = inspect.getsource(followup_voice_mod)
    for forbidden in ("httpx", "MessagePort", "OpenAI"):
        assert forbidden not in source


def test_followups_module_has_no_message_port() -> None:
    import app.domain.followups as followups_mod

    source = inspect.getsource(followups_mod)
    assert "MessagePort" not in source


def test_follow_up_claim_key_format() -> None:
    assert follow_up_claim_key("wamid.fu.claim.3") == "wamid.fu.claim.3:followup"


def test_claim_follow_up_persist_empty_inbound_id_returns_false() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert claim_follow_up_persist(store=store, inbound_id="") is False
        assert store.get_operation_result(scope=FOLLOW_UP_SCOPE, key=":followup") == "{}"
    finally:
        db.close()


def test_claim_follow_up_persist_first_true_complete_second_false() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        inbound_id = "wamid.fu.helper.claim.1"
        assert claim_follow_up_persist(store=store, inbound_id=inbound_id) is True
        complete_follow_up_persist(store=store, inbound_id=inbound_id)
        assert claim_follow_up_persist(store=store, inbound_id=inbound_id) is False
        result = store.get_operation_result(
            scope=FOLLOW_UP_SCOPE,
            key=follow_up_claim_key(inbound_id),
        )
        assert json.loads(result) == {"ok": True}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_failed_webhook_retry_skips_second_follow_up_upsert() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        messages = [
            "We run a clinic and miss calls all day.",
            "ok that's right",
            "I decide this quarter",
            "let's book a meeting",
        ]
        lead_id = ""
        for index, text in enumerate(messages):
            await process_inbound_texts(
                provider="whatsapp",
                channel=Channel.WHATSAPP,
                items=[{"id": f"wamid.fu.claim.{index}", "from": CLAIM_PHONE, "text": text}],
                store=store,
                port=port,
                kill_switch=False,
                calendar=DisabledCalendarPort(),
                sheets=DisabledSheetsPort(),
            )
            db.commit()
            _, lead_id = store.open_channel_lead(
                channel=Channel.WHATSAPP, external_id=CLAIM_PHONE
            )
        row = store.get_follow_up(lead_id)
        assert row is not None
        assert row.status == STATUS_PENDING
        db_row = db.scalars(
            select(FollowUpRow).where(FollowUpRow.lead_id == lead_id)
        ).one()
        db_row.due_at = "2099-01-01"
        db.commit()

        store.mark_webhook(
            provider="whatsapp",
            provider_event_id="wamid.fu.claim.3",
            status="failed",
        )
        db.commit()

        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[{"id": "wamid.fu.claim.3", "from": CLAIM_PHONE, "text": "let's book a meeting"}],
            store=store,
            port=port,
            kill_switch=False,
            calendar=DisabledCalendarPort(),
            sheets=DisabledSheetsPort(),
        )
        db.commit()
        row = store.get_follow_up(lead_id)
        assert row is not None
        assert row.status == STATUS_PENDING
        assert row.due_at == "2099-01-01"
        result = store.get_operation_result(
            scope=FOLLOW_UP_SCOPE,
            key=follow_up_claim_key("wamid.fu.claim.3"),
        )
        assert json.loads(result) == {"ok": True}
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_prospect_offer_meeting_creates_follow_up() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        messages = [
            "We run a clinic and miss calls all day.",
            "ok that's right",
            "I decide this quarter",
            "let's book a meeting",
        ]
        lead_id = ""
        for index, text in enumerate(messages):
            await process_inbound_texts(
                provider="whatsapp",
                channel=Channel.WHATSAPP,
                items=[{"id": f"wamid.fu.{index}", "from": PROSPECT_PHONE_2, "text": text}],
                store=store,
                port=port,
                kill_switch=False,
                calendar=DisabledCalendarPort(),
                sheets=DisabledSheetsPort(),
            )
            db.commit()
            _, lead_id = store.open_channel_lead(
                channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE_2
            )
        row = store.get_follow_up(lead_id)
        assert row is not None
        assert row.status == STATUS_PENDING
        assert row.channel == Channel.WHATSAPP.value
    finally:
        db.close()


@pytest.mark.asyncio
async def test_inbound_stop_cancels_follow_up() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        funnel = [
            "We run a clinic and miss calls all day.",
            "ok that's right",
            "I decide this quarter",
            "let's book a meeting",
            "not interested",
        ]
        for index, text in enumerate(funnel):
            await process_inbound_texts(
                provider="whatsapp",
                channel=Channel.WHATSAPP,
                items=[{"id": f"wamid.stop.{index}", "from": PROSPECT_PHONE_STOP, "text": text}],
                store=store,
                port=port,
                kill_switch=False,
                calendar=DisabledCalendarPort(),
                sheets=DisabledSheetsPort(),
            )
            db.commit()
        _, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id=PROSPECT_PHONE_STOP
        )
        row = store.get_follow_up(lead_id)
        assert row is not None
        assert row.status == STATUS_CANCELLED
    finally:
        db.close()
