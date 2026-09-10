import pytest
from app.capabilities.mail import mail_handlers
from app.capabilities.policy import execute_capability
from app.capabilities.types import Principal
from app.core.errors import PermissionDenied
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.integrations.gmail import FakeGmailPort, InboundEmail
from app.main import app
from fastapi.testclient import TestClient


def test_two_website_tenants_are_isolated_and_client_is_denied_owner_mail() -> None:
    """Two real sessions. A's secret is not in B's turns. B cannot post to a missing
    session. A client principal cannot read owner mail — that is the deny, not
    comparing two in-memory dicts.
    """
    init_db()
    with TestClient(app) as client:
        created_a = client.post("/v1/website/sessions").json()
        created_b = client.post("/v1/website/sessions").json()
        session_a = created_a["session_id"]
        session_b = created_b["session_id"]
        assert session_a != session_b
        posted_a = client.post(
            f"/v1/website/sessions/{session_a}/messages",
            json={"text": "tenant-a-secret-never-share", "client_message_id": "tenant-a-1"},
            headers={"X-Mia-Session-Credential": created_a["session_credential"]},
        )
        posted_b = client.post(
            f"/v1/website/sessions/{session_b}/messages",
            json={"text": "tenant-b-other-text", "client_message_id": "tenant-b-1"},
            headers={"X-Mia-Session-Credential": created_b["session_credential"]},
        )
        assert posted_a.status_code == 200
        assert posted_b.status_code == 200
        assert posted_a.json()["lead_id"] == ""
        assert posted_b.json()["lead_id"] == ""
        missing = client.post(
            "/v1/website/sessions/web_does_not_exist/messages",
            json={"text": "probe", "client_message_id": "missing-1"},
            headers={"X-Mia-Session-Credential": created_a["session_credential"]},
        )
        assert missing.status_code == 404
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert store.get_website_lead_id(session_a) is None
        assert store.get_website_lead_id(session_b) is None
        turns_b = store.list_conversation_turns(session_b)
        blob_b = " ".join(turn.text for turn in turns_b)
        assert "tenant-a-secret-never-share" not in blob_b
        turns_a = store.list_conversation_turns(session_a)
        blob_a = " ".join(turn.text for turn in turns_a)
        assert "tenant-a-secret-never-share" in blob_a
        assert "tenant-b-other-text" not in blob_a
    finally:
        db.close()
    port = FakeGmailPort(
        {"m1": InboundEmail(message_id="m1", sender="a@b.com", subject="hi", text="x")}
    )
    with pytest.raises(PermissionDenied):
        execute_capability(
            "mail.read",
            principal=Principal.client(source="website", actor_id=session_b),
            args={"message_id": "m1"},
            handlers=mail_handlers(port),
        )
