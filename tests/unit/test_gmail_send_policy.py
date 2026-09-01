"""Owner-requested Gmail send stays; unsolicited send and delete-forever stay denied."""

from pathlib import Path

import pytest
from app.capabilities.policy import authorize
from app.capabilities.types import Principal
from app.core.config import Settings
from app.core.errors import PermissionDenied
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.approvals import DECISION_APPROVED
from app.domain.events import Channel
from app.domain.gmail_drafts import (
    apply_gmail_send_decision,
    apply_owner_gmail_draft,
    execute_approved_gmail_send,
)
from app.integrations.composio_catalog import (
    DENIED_COMPOSIO_SLUGS,
    OWNER_REQUESTED_GMAIL_SEND_SLUGS,
    risk_for_slug,
)
from app.integrations.gmail import FakeGmailPort
from app.tools.registries.owner_tools import tool_names

_APP = Path(__file__).resolve().parents[2] / "app"
_NO_GMAIL_SEND_MODULES = (
    _APP / "api" / "website.py",
    _APP / "domain" / "website_handoff_brief.py",
    _APP / "workers" / "due_scan.py",
    _APP / "agents" / "client" / "graph.py",
)


def test_official_gmail_send_slugs_are_not_denied() -> None:
    assert "GMAIL_SEND" not in OWNER_REQUESTED_GMAIL_SEND_SLUGS
    assert "GMAIL_SEND" not in DENIED_COMPOSIO_SLUGS
    assert OWNER_REQUESTED_GMAIL_SEND_SLUGS.isdisjoint(DENIED_COMPOSIO_SLUGS)
    assert OWNER_REQUESTED_GMAIL_SEND_SLUGS == {
        "GMAIL_SEND_EMAIL",
        "GMAIL_SEND_DRAFT",
        "GMAIL_REPLY_TO_THREAD",
        "GMAIL_FORWARD_MESSAGE",
    }


def test_delete_forever_class_is_denied_and_unofficial_forever_slug_is_r5() -> None:
    for slug in (
        "GMAIL_DELETE_MESSAGE",
        "GMAIL_BATCH_DELETE_MESSAGES",
        "GMAIL_DELETE_THREAD",
        "GMAIL_DELETE_DRAFT",
        "GMAIL_DELETE_FILTER",
        "GMAIL_DELETE_LABEL",
        "GOOGLE_SEARCH_CONSOLE_DELETE_SITE",
    ):
        assert slug in DENIED_COMPOSIO_SLUGS
        assert risk_for_slug(slug).value == "R5"
    # Official catalog has no GMAIL_DELETE_FOREVER; DELETE still classifies R5.
    assert "GMAIL_DELETE_FOREVER" not in DENIED_COMPOSIO_SLUGS
    assert risk_for_slug("GMAIL_DELETE_FOREVER").value == "R5"


def test_llm_owner_registry_still_has_no_gmail_send_tool() -> None:
    names = tool_names()
    assert "gmail_inbox" in names
    assert "gmail_send" not in names
    assert "gmail_delete" not in names


def test_visitor_and_due_scan_cannot_authorize_mail_send() -> None:
    for source in ("website", "due_scan", "handoff"):
        visitor = Principal.client(source=source)
        with pytest.raises(PermissionDenied):
            authorize("mail.send", principal=visitor, preapproved=True)
        with pytest.raises(PermissionDenied):
            authorize("mail.create_draft", principal=visitor)


def test_website_handoff_and_due_scan_modules_cannot_send_mail() -> None:
    for path in _NO_GMAIL_SEND_MODULES:
        source = path.read_text(encoding="utf-8")
        assert "send_draft" not in source
        assert "GMAIL_SEND" not in source
        assert "execute_approved_gmail_send" not in source
        assert "apply_owner_gmail_draft" not in source


def test_owner_telegram_asked_then_approved_send_calls_send_draft() -> None:
    init_db()
    session = get_session_factory()()
    try:
        store = LeadStore(session)
        port = FakeGmailPort()
        ack = apply_owner_gmail_draft(
            store,
            text="שלח מייל ל dane@example.com נושא: היי והתוכן שלום",
            channel=Channel.TELEGRAM,
            port=port,
            kill_switch=False,
            demo_active=False,
        )
        session.commit()
        assert "טיוטה מוכנה" in ack
        assert port.sent_drafts == []
        decision, draft_id = apply_gmail_send_decision(
            store,
            text="אשר את המייל",
            kill_switch=False,
        )
        session.commit()
        assert decision == DECISION_APPROVED
        assert draft_id == port.created_drafts[0].draft_id
        sent = execute_approved_gmail_send(
            store=store,
            settings=Settings(gmail_send=True),
            port=port,
            draft_id=draft_id,
            kill_switch=False,
            demo_active=False,
        )
        assert sent == "שלחתי את המייל."
        assert port.sent_drafts == [draft_id]
    finally:
        session.close()
