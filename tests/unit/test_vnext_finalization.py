from datetime import UTC, datetime, timedelta

from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel, build_message_in_event
from app.services.finalization import (
    ConversationSummary,
    finalize_website_conversation,
    kind_for,
    qualify_and_finalize,
    scan_inactive_website_conversations,
)
from app.services.notifications import render_conversation_summary


class _MemStore:
    def __init__(self) -> None:
        self.rows: set[tuple[str, str]] = set()

    def has_owner_notification(self, *, kind: str, lead_id: str) -> bool:
        return (kind, lead_id) in self.rows

    def try_insert_owner_notification(
        self, *, kind: str, lead_id: str, scheduled_at: str
    ) -> bool:
        key = (kind, lead_id)
        if key in self.rows:
            return False
        self.rows.add(key)
        return True


def test_summary_omits_empty_fields() -> None:
    text = render_conversation_summary(
        {
            "name": "Dana",
            "budget": None,
            "conversation_id": "web_1",
        }
    )
    assert "Name: Dana" in text
    assert "Budget" not in text
    assert "Conversation ID: web_1" in text


def test_finalization_is_idempotent() -> None:
    store = _MemStore()
    settings = Settings()
    summary = ConversationSummary(
        conversation_id="web_1",
        lead_id="lead_abc",
        name="Dana",
        recommended_next_step="call",
    )
    first = finalize_website_conversation(store, summary=summary, settings=settings)
    second = finalize_website_conversation(store, summary=summary, settings=settings)
    assert first.claimed is True
    assert first.duplicate is False
    assert second.claimed is False
    assert second.duplicate is True
    assert second.sent is False
    assert first.kind == kind_for("v1")
    assert len(store.rows) == 1


def test_inactive_website_conversation_finalizes_once() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_inactive01abcd"
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=session_id
        )
        old = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        store.save_canonical_event(
            provider="website",
            event=build_message_in_event(
                provider="website",
                channel=Channel.WEBSITE,
                provider_event_id="in.old",
                conversation_id=session_id,
                text="שלום",
                actor_role="prospect",
                lead_id=lead_id,
                occurred_at=old,
            ),
        )
        db.commit()
        settings = Settings(website_inactivity_minutes=30)
        now = old + timedelta(minutes=31)
        assert scan_inactive_website_conversations(store, settings=settings, now=now) == 1
        db.commit()
        assert scan_inactive_website_conversations(store, settings=settings, now=now) == 0
        assert store.has_owner_notification(kind=kind_for(), lead_id=lead_id)
    finally:
        db.close()


def test_empty_website_session_is_not_finalized_on_inactivity() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_empty01abcdef"
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=session_id
        )
        db.commit()
        result = qualify_and_finalize(
            store,
            session_id=session_id,
            lead_id=lead_id,
            settings=Settings(),
            next_step="inactivity",
            require_visitor_message=True,
        )
        assert result is None
        assert not store.has_owner_notification(kind=kind_for(), lead_id=lead_id)
    finally:
        db.close()

