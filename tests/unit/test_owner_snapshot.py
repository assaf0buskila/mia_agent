"""Operator snapshot: grounded Postgres facts, no command menu, no writes."""

from app.capabilities.types import Principal
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.owner.snapshot import format_operator_snapshot_ack
from app.domain.owner.status import format_owner_status_ack


def test_operator_snapshot_has_facts_and_no_menu() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        ack = format_operator_snapshot_ack(
            store,
            principal=Principal.owner(source="test"),
            timezone="Asia/Jerusalem",
        )
        digest = format_owner_status_ack(
            store,
            principal=Principal.owner(source="test"),
            timezone="Asia/Jerusalem",
        )
        assert "לא כתבתי כלום" in ack
        assert "אפשר לבקש" not in ack
        assert "קונסולת הבעלים" not in ack
        assert ack != digest
        assert "היום:" in ack or "לידים חמים" in ack or "לאישור" in ack
        assert "COMPOSIO" not in ack
        assert "gmail.users" not in ack
    finally:
        db.close()


def test_combined_snapshot_includes_only_asked_read_facts() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        ack = format_operator_snapshot_ack(
            store,
            principal=Principal.owner(source="test"),
            timezone="Asia/Jerusalem",
            matched_types=["daily_brief", "hot_leads"],
        )
        assert "היום:" in ack
        assert "לידים חמים" in ack
        assert "לא כתבתי כלום" in ack
        assert "אפשר לבקש" not in ack
        assert "שיחות מהאתר" not in ack
    finally:
        db.close()
