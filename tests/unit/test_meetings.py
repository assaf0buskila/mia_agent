import importlib

from app.db.models import MeetingRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.meetings import STATUS_OFFERED, apply_meeting_policy
from app.domain.sales import NextAction
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select

WEB_SESSION_SHEETS = "web_meet_sheet_997009"


def _meeting_for_lead(db, lead_id: str) -> MeetingRow | None:
    return db.scalars(select(MeetingRow).where(MeetingRow.lead_id == lead_id)).one_or_none()


def test_website_identify_then_sell_does_not_persist_meetings() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        clinic = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "We run a clinic and miss calls all day."},
        )
        assert clinic.status_code == 200
        assert clinic.json()["next_action"] == "answer"
        meeting = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "let's book a meeting", "phone": "0501234567"},
        )
        assert meeting.json()["next_action"] in {"handoff", "confirm_contact"}
        assert meeting.json()["lead_id"] == ""
    db = get_session_factory()()
    try:
        assert _meeting_for_lead(db, session_id) is None
        assert LeadStore(db).get_website_lead_id(session_id) is None
    finally:
        db.close()


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


def test_handoff_does_not_persist_meeting() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "Please send me a proposal", "phone": "0501234567"},
        )
        assert response.status_code == 200
        assert response.json()["next_action"] in {"handoff", "confirm_contact"}
        assert response.json()["lead_id"] == ""
    db = get_session_factory()()
    try:
        assert _meeting_for_lead(db, session_id) is None
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
    meetings = importlib.import_module("app.domain.meetings")
    source = importlib.import_module("inspect").getsource(meetings)
    assert "MessagePort" not in source
    assert "integrations.base" not in source


def test_env_kill_switch_does_not_503_website_and_does_not_persist_meeting(
    monkeypatch,
) -> None:
    monkeypatch.setenv("MIA_KILL_SWITCH", "true")
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        response = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "We run a clinic and miss calls all day."},
        )
        assert response.status_code == 200
        assert response.json()["next_action"] in {"ask_contact", "answer", "ask_need"}
    db = get_session_factory()()
    try:
        assert LeadStore(db).get_website_lead_id(session_id) is None
        assert _meeting_for_lead(db, session_id) is None
    finally:
        db.close()
