from datetime import UTC, datetime

from app.db.models import ToolRunRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.conversation_kill import opt_out_status_outcome
from app.main import app
from fastapi.testclient import TestClient
from sqlalchemy import select


def test_opt_out_status_outcome_live_when_present() -> None:
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    outcome = opt_out_status_outcome(present=True, now=now)
    assert outcome.freshness == "live"
    assert outcome.status == "ok"
    assert outcome.result_count == 1


def _opt_out_rows(db, lead_id: str) -> list[ToolRunRow]:
    return list(
        db.scalars(
            select(ToolRunRow).where(
                ToolRunRow.lead_id == lead_id,
                ToolRunRow.tool == "opt_out_status",
            )
        )
    )


def test_website_identify_then_sell_does_not_create_opt_out_tool_run() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        clinic = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "We run a clinic and miss calls all day."},
        )
        assert clinic.status_code == 200
        assert clinic.json()["next_action"] == "answer"
        stop = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "not interested"},
        )
        assert stop.status_code == 200
        assert stop.json()["next_action"] in {"ask_contact", "answer"}
        recover = client.post(
            f"/v1/website/sessions/{session_id}/messages",
            json={"text": "let's book a meeting", "phone": "0501234567"},
        )
        assert recover.json()["next_action"] == "handoff"
        assert recover.json()["lead_id"] == ""
    db = get_session_factory()()
    try:
        assert _opt_out_rows(db, "") == []
        assert LeadStore(db).is_conversation_killed("") is False
    finally:
        db.close()
