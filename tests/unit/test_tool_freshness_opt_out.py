from datetime import UTC, datetime

from app.db.models import ToolRunRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.conversation_kill import opt_out_status_outcome
from app.domain.sales import NextAction
from app.graph.replies import WEBSITE_REPLIES_EN
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select

# The fixture conversation below is English, so Mia answers in English.
STOP_REPLY = WEBSITE_REPLIES_EN[NextAction.STOP]


def test_opt_out_status_outcome_live_when_present() -> None:
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    outcome = opt_out_status_outcome(present=True, now=now)
    assert outcome.freshness == "live"
    assert outcome.status == "ok"
    assert outcome.result_count == 1


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
        lead_id = response.json()["lead_id"]
    assert response.json()["next_action"] == "offer_meeting"
    return lead_id


def _opt_out_rows(db, lead_id: str) -> list[ToolRunRow]:
    return list(
        db.scalars(
            select(ToolRunRow).where(
                ToolRunRow.lead_id == lead_id,
                ToolRunRow.tool == "opt_out_status",
            )
        )
    )


def test_website_stop_creates_opt_out_status_freshness_live() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        _run_clinic_funnel_to_meeting(client, session_id)
        stop = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "not interested"},
        )
        assert stop.status_code == 200
        assert stop.json()["next_action"] == "stop"
        assert stop.json()["message"] == STOP_REPLY
        lead_id = stop.json()["lead_id"]
    db = get_session_factory()()
    try:
        rows = _opt_out_rows(db, lead_id)
        assert len(rows) == 1
        assert rows[0].freshness == "live"
        assert rows[0].status == "ok"
    finally:
        db.close()


def test_website_stop_recover_stamps_opt_out_live_again() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "not interested"},
        )
        recover = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "let's book a meeting"},
        )
        assert recover.json()["next_action"] == "qualify"
        lead_id = recover.json()["lead_id"]
    db = get_session_factory()()
    try:
        rows = _opt_out_rows(db, lead_id)
        assert len(rows) == 2
        assert all(row.freshness == "live" for row in rows)
        assert LeadStore(db).is_conversation_killed(lead_id) is False
    finally:
        db.close()


def test_qualify_funnel_does_not_create_opt_out_tool_run() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        lead_id = _run_clinic_funnel_to_meeting(client, session_id)
    db = get_session_factory()()
    try:
        rows = _opt_out_rows(db, lead_id)
        assert rows == []
        assert LeadStore(db).is_conversation_killed(lead_id) is False
    finally:
        db.close()


def test_website_stop_reply_unchanged() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        _run_clinic_funnel_to_meeting(client, session_id)
        stop = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "not interested"},
        )
        assert stop.json()["message"] == STOP_REPLY
