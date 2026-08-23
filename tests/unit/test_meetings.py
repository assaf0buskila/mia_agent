import importlib

from app.api.deps import get_sheets_port
from app.db.models import MeetingRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.meetings import STATUS_OFFERED, apply_meeting_policy
from app.domain.sales import NextAction
from app.integrations.sheets import FakeSheetsPort
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select

WEB_SESSION_SHEETS = "web_meet_sheet_997009"


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
    assert body["next_action"] == NextAction.OFFER_MEETING.value
    return lead_id


def _meeting_for_lead(db, lead_id: str) -> MeetingRow | None:
    return db.scalars(select(MeetingRow).where(MeetingRow.lead_id == lead_id)).one_or_none()


def test_offer_meeting_persists_meeting_offered_empty_fields() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        lead_id = _run_clinic_funnel_to_meeting(client, session_id)
    db = get_session_factory()()
    try:
        row = _meeting_for_lead(db, lead_id)
        assert row is not None
        assert row.status == STATUS_OFFERED
        assert row.source == Channel.WEBSITE.value
        assert row.scheduled_at == ""
        assert row.calendar_event_id == ""
        assert row.summary == ""
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
            json={"text": "Please send me a proposal"},
        )
        assert response.status_code == 200
        assert response.json()["next_action"] == NextAction.HANDOFF.value
        lead_id = response.json()["lead_id"]
    db = get_session_factory()()
    try:
        assert _meeting_for_lead(db, lead_id) is None
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


def test_fake_sheets_port_after_offer_meeting_has_meeting_row_empty_fields() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = WEB_SESSION_SHEETS
        store.open_channel_lead(channel=Channel.WEBSITE, external_id=session_id)
        db.commit()

        fake = FakeSheetsPort()
        app.dependency_overrides[get_sheets_port] = lambda: fake
        try:
            with TestClient(app) as client:
                lead_id = _run_clinic_funnel_to_meeting(client, session_id)
                assert lead_id in fake.meeting_rows
                meeting = fake.meeting_rows[lead_id]
                assert meeting.status == STATUS_OFFERED
                assert meeting.source == Channel.WEBSITE.value
                assert meeting.scheduled_at == ""
                assert meeting.calendar_event_id == ""
                assert meeting.summary == ""
        finally:
            app.dependency_overrides.pop(get_sheets_port, None)
    finally:
        db.close()


def test_env_kill_switch_skips_meeting_persist(monkeypatch) -> None:
    monkeypatch.setenv("MIA_KILL_SWITCH", "true")
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        lead_id = _run_clinic_funnel_to_meeting(client, session_id)
    db = get_session_factory()()
    try:
        assert _meeting_for_lead(db, lead_id) is None
    finally:
        db.close()
