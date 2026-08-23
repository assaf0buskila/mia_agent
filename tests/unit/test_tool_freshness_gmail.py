import base64
import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.integrations.gmail import (
    DisabledGmailPort,
    FakeGmailPort,
    InboundEmail,
    gmail_results_outcome,
)
from app.main import app
from fastapi.testclient import TestClient

COMPOSIO_SECRET = "composio-webhook-secret"
GMAIL_TRIGGER = "GMAIL_NEW_GMAIL_MESSAGE"


def _sign_composio_payload(
    payload: dict,
    *,
    secret: str = COMPOSIO_SECRET,
    webhook_id: str = "wh_fresh_gmail",
    timestamp: int | None = None,
) -> tuple[bytes, dict[str, str]]:
    ts = timestamp if timestamp is not None else int(time.time())
    raw = json.dumps(payload, separators=(",", ":")).encode()
    signed = f"{webhook_id}.{ts}.".encode() + raw
    digest = base64.b64encode(
        hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).digest()
    ).decode("ascii")
    headers = {
        "Content-Type": "application/json",
        "webhook-id": webhook_id,
        "webhook-timestamp": str(ts),
        "webhook-signature": f"v1,{digest}",
    }
    return raw, headers


def _gmail_trigger_payload(
    *,
    message_id: str,
    sender: str = "Fresh Lead <fresh.gmail.cached@example.com>",
    subject: str = "",
    message_text: str = "",
    thread_id: str = "thread_fresh_gmail",
) -> dict:
    return {
        "type": "composio.trigger.message",
        "metadata": {"trigger_slug": GMAIL_TRIGGER},
        "data": {
            "message_id": message_id,
            "sender": sender,
            "subject": subject,
            "message_text": message_text,
            "thread_id": thread_id,
        },
    }


def test_gmail_results_outcome_cached_when_present() -> None:
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    outcome = gmail_results_outcome(present=True, now=now)
    assert outcome.freshness == "cached"
    assert outcome.status == "ok"
    assert outcome.result_count == 1


def test_gmail_results_outcome_unverified_when_empty() -> None:
    now = datetime(2026, 8, 21, 12, 0, 0, tzinfo=UTC)
    outcome = gmail_results_outcome(present=False, now=now)
    assert outcome.freshness == "unverified"
    assert outcome.status == "empty"
    assert outcome.result_count == 0


def test_gmail_webhook_empty_body_fake_port_freshness_cached(monkeypatch) -> None:
    monkeypatch.setenv("MIA_COMPOSIO_WEBHOOK_SECRET", COMPOSIO_SECRET)
    message_id = "msg.fresh.gmail.cached.1"
    fake_port = FakeGmailPort(
        {
            message_id: InboundEmail(
                message_id=message_id,
                sender="fresh.gmail@example.com",
                subject="Hydrated subject",
                text="Hydrated body",
                thread_id="thread_fresh_cached",
            )
        }
    )
    monkeypatch.setattr("app.api.composio.build_gmail_port", lambda _settings: fake_port)
    payload = _gmail_trigger_payload(message_id=message_id)
    raw, headers = _sign_composio_payload(payload)
    with TestClient(app) as client:
        response = client.post("/v1/composio/webhook", content=raw, headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["processed"] == 1
        assert body["sent"] is False
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        inbound = store.get_canonical_event(provider="gmail", provider_event_id=message_id)
        row = store.get_tool_run(f"{message_id}:tool:gmail_fetch")
        assert row is not None
        assert row.freshness == "cached"
        assert row.status == "ok"
        assert inbound is not None
        assert row.correlation_id == inbound.correlation_id
        assert inbound.correlation_id.startswith("run_")
    finally:
        db.close()


def test_gmail_webhook_empty_body_disabled_freshness_unverified(monkeypatch) -> None:
    monkeypatch.setenv("MIA_COMPOSIO_WEBHOOK_SECRET", COMPOSIO_SECRET)
    monkeypatch.setattr(
        "app.api.composio.build_gmail_port",
        lambda _settings: DisabledGmailPort(),
    )
    message_id = "msg.fresh.gmail.unverified.1"
    payload = _gmail_trigger_payload(
        message_id=message_id,
        sender="Fresh Lead <fresh.gmail.unverified@example.com>",
    )
    raw, headers = _sign_composio_payload(payload)
    with TestClient(app) as client:
        response = client.post("/v1/composio/webhook", content=raw, headers=headers)
        assert response.status_code == 200
        assert response.json()["processed"] == 1
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        row = store.get_tool_run(f"{message_id}:tool:gmail_fetch")
        assert row is not None
        assert row.freshness == "unverified"
        assert row.status == "empty"
    finally:
        db.close()


def test_gmail_webhook_nonempty_body_no_gmail_fetch_row(monkeypatch) -> None:
    monkeypatch.setenv("MIA_COMPOSIO_WEBHOOK_SECRET", COMPOSIO_SECRET)

    class RaisingGmailPort:
        def fetch_message(self, message_id: str) -> None:
            del message_id
            raise RuntimeError("port must not be called when trigger body is non-empty")

    monkeypatch.setattr(
        "app.api.composio.build_gmail_port",
        lambda _settings: RaisingGmailPort(),
    )
    message_id = "msg.fresh.gmail.nostamp.1"
    payload = _gmail_trigger_payload(
        message_id=message_id,
        sender="Fresh Lead <fresh.gmail.nostamp@example.com>",
        subject="Hello",
        message_text="already here",
    )
    raw, headers = _sign_composio_payload(payload)
    with TestClient(app) as client:
        response = client.post("/v1/composio/webhook", content=raw, headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["processed"] == 1
        assert body["sent"] is False
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert store.get_tool_run(f"{message_id}:tool:gmail_fetch") is None
    finally:
        db.close()
