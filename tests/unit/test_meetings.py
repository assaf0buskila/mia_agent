import importlib

from app.db.models import MeetingRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.meetings.state import STATUS_OFFERED, apply_meeting_policy
from app.domain.sales import NextAction
from sqlalchemy import select

WEB_SESSION_SHEETS = "web_meet_sheet_997009"


def _meeting_for_lead(db, lead_id: str) -> MeetingRow | None:
    return db.scalars(select(MeetingRow).where(MeetingRow.lead_id == lead_id)).one_or_none()


def test_kill_switch_skips_meeting_persist() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_meet_kill_997202"
        )
        apply_meeting_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            kill_switch=True,
        )
        db.commit()
        assert store.get_meeting(lead_id) is None
    finally:
        db.close()


def test_stop_action_does_not_persist_meeting() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_meet_stop_997203"
        )
        apply_meeting_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.STOP.value,
            kill_switch=False,
        )
        db.commit()
        assert store.get_meeting(lead_id) is None
    finally:
        db.close()


def test_reoffer_is_idempotent_one_row_per_lead() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_meet_reoffer_997204"
        )
        apply_meeting_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            kill_switch=False,
        )
        apply_meeting_policy(
            store,
            lead_id=lead_id,
            channel=Channel.WEBSITE,
            action=NextAction.OFFER_MEETING.value,
            kill_switch=False,
        )
        db.commit()
        rows = list(
            db.scalars(select(MeetingRow).where(MeetingRow.lead_id == lead_id)).all()
        )
        assert len(rows) == 1
        assert rows[0].status == STATUS_OFFERED
        assert rows[0].scheduled_at == ""
        assert rows[0].calendar_event_id == ""
        assert rows[0].summary == ""
    finally:
        db.close()


def test_meetings_module_never_imports_message_port() -> None:
    meetings = importlib.import_module("app.domain.meetings.state")
    source = importlib.import_module("inspect").getsource(meetings)
    assert "MessagePort" not in source
    assert "integrations.base" not in source
