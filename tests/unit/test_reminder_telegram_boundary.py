"""Combined Privacy/reminder webhook boundary; all Telegram operations are fake."""

import os
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from app.api import telegram as api
from app.api.deps import get_telegram_port, get_transcription_port
from app.core.config import Settings
from app.db.base import Base
from app.db.models import CrmContactRow
from app.db.reminders import OwnerReminderRow
from app.integrations.base import RecordingMessagePort
from app.integrations.transcribe import FakeTranscriptionPort
from app.services.reminders import ReminderService
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

OWNER = "123"
SECRET = "boundary-secret"


class Port(RecordingMessagePort):
    def __init__(self):
        super().__init__()
        self.acks = []

    async def answer_callback_query(self, callback_id):
        self.acks.append(callback_id)


def payload(data):
    return {
        "update_id": 901,
        "callback_query": {
            "id": "callback-1",
            "from": {"id": 123},
            "data": data,
            "message": {"message_id": 12, "chat": {"id": 123, "type": "private"}},
        },
    }


def post(monkeypatch, update, port, *, enabled=True):
    settings = Settings(
        _env_file=None,
        telegram_owner_user_ids="123,456",
        telegram_webhook_secret=SECRET,
        reminders_enabled=enabled,
    )
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[get_telegram_port] = lambda: port
    app.dependency_overrides[get_transcription_port] = lambda: FakeTranscriptionPort("unused")
    with TestClient(app) as client:
        response = client.post(
            "/v1/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}
        )
    assert response.status_code == 200
    return response.json()


@pytest.mark.parametrize("action", ["complete", "snooze", "contact", "malformed"])
@pytest.mark.parametrize(
    "rejection",
    ["unauthorized", "group", "mismatched", "missing_type", "invalid_numeric_id", "inline"],
)
def test_all_reminder_callbacks_authorize_before_db_ack_or_business(monkeypatch, action, rejection):
    from app.tools.owner import reminders

    def forbidden(*args, **kwargs):
        pytest.fail("rejected callback reached database or reminder business work")

    monkeypatch.setattr(api, "get_db", forbidden)
    monkeypatch.setattr(reminders, "handle_reminder_callback", forbidden)
    update = payload(f"rm:{action}:{'a' * 32}:1")
    callback = update["callback_query"]
    chat = callback["message"]["chat"]
    if rejection == "unauthorized":
        callback["from"]["id"] = chat["id"] = 999
    elif rejection == "group":
        chat.update(id=-100, type="supergroup")
    elif rejection == "mismatched":
        chat["id"] = 456
    elif rejection == "missing_type":
        chat.pop("type")
    elif rejection == "invalid_numeric_id":
        callback["from"]["id"] = True
    else:
        callback.pop("message")
        callback["inline_message_id"] = "inline"
    port = Port()
    assert post(monkeypatch, update, port)["ignored"] is True
    assert port.acks == [] and port.sent == []


def test_malformed_private_reminder_callback_does_not_open_db(monkeypatch):
    def forbidden():
        pytest.fail("malformed callback opened database")

    monkeypatch.setattr(api, "get_db", forbidden)
    port = Port()
    result = post(monkeypatch, payload("rm:complete:not-an-id:1"), port)
    assert result["reason"] == "unrecognized_callback"
    assert port.acks == ["callback-1"] and port.sent == []


@pytest.fixture(params=["sqlite", "postgres"])
def factory(tmp_path, request):
    admin = None
    schema = "reminder_http_" + uuid4().hex
    if request.param == "postgres":
        url = os.environ.get("MIA_TEST_POSTGRES_URL", "")
        if not url:
            pytest.skip("MIA_TEST_POSTGRES_URL required")
        admin = create_engine(url)
        with admin.begin() as conn:
            conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema},public"})
    else:
        engine = create_engine(
            f"sqlite:///{tmp_path / 'http.db'}", connect_args={"check_same_thread": False}
        )
    Base.metadata.create_all(engine, checkfirst=request.param != "postgres")
    yield sessionmaker(engine, expire_on_commit=False)
    engine.dispose()
    if admin is not None:
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.parametrize("action", ["complete", "snooze", "contact"])
def test_private_owner_reminder_http_uses_real_session_and_preserves_revision(
    monkeypatch, factory, action
):
    now = datetime.now(UTC)
    with factory() as db:
        db.add(CrmContactRow(id="danny", fields_json='{"name": "Danny"}'))
        db.commit()
        row = (
            ReminderService(db)
            .create(
                owner_id=OWNER,
                source_event_id="http",
                text="Call Danny",
                scheduled_at=now + timedelta(days=1),
                contact_query="danny",
                now=now,
            )
            .reminder
        )
        reminder_id = row.id

    opened = []

    def get_db():
        opened.append(True)
        with factory() as db:
            yield db
            db.commit()

    monkeypatch.setattr(api, "get_db", get_db)
    port = Port()
    update = payload(f"rm:{action}:{reminder_id}:1")
    assert post(monkeypatch, update, port) == {"processed": 1, "sent": True}
    assert len(opened) == 1 and port.acks == ["callback-1"]
    assert len(port.sent) == 1 and port.sent[0].conversation_id == OWNER
    with factory() as db:
        row = db.get(OwnerReminderRow, reminder_id)
        assert row.revision == (1 if action == "contact" else 2)
        assert row.status == ("completed" if action == "complete" else "active")
        if action == "snooze":
            due = (
                row.scheduled_at.replace(tzinfo=UTC)
                if row.scheduled_at.tzinfo is None
                else row.scheduled_at
            )
            assert timedelta(minutes=14) < due - now < timedelta(minutes=16)
        if action == "contact":
            assert "Danny" in port.sent[0].text
    # Replay cannot mutate a newer revision or revive a completed reminder.
    post(monkeypatch, update, port)
    with factory() as db:
        assert db.get(OwnerReminderRow, reminder_id).revision == (1 if action == "contact" else 2)
