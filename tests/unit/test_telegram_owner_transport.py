"""Telegram is transport only: the entry point and the webhook claim.

Everything this file used to assert about OwnerGraph went away with the graph itself
(v2 cleanup). What is left is the part that is still load-bearing and still costs
something: the owner update must enter through the deferred worker, and a caller must
not be able to hand `process_owner_texts` an event id it never actually received.
"""

from __future__ import annotations

from app.api import telegram as telegram_api
from app.api.owner import process_owner_texts
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.integrations.base import RecordingMessagePort

OWNER_ID = "550077"


def test_telegram_owner_entry_is_deferred_worker() -> None:
    from app.workers.telegram_owner import process_telegram_owner_update

    assert telegram_api.process_telegram_owner_update is process_telegram_owner_update


async def test_preclaimed_owner_event_requires_the_exact_received_webhook() -> None:
    """Caller-controlled item data cannot bypass the canonical webhook claim."""
    init_db()
    session = get_session_factory()()
    port = RecordingMessagePort()
    try:
        result = await process_owner_texts(
            provider="telegram",
            channel=Channel.TELEGRAM,
            items=[{"id": "evt.owner.unclaimed.1", "from": OWNER_ID, "text": "שלום"}],
            store=LeadStore(session),
            port=port,
            kill_switch=False,
            owner_ids={OWNER_ID},
            preclaimed_event_id="evt.owner.unclaimed.1",
            preclaimed_envelope_kind="audio",
        )
    finally:
        session.close()

    assert result["processed"] == 0
    assert result["duplicates"] == 1
    assert port.sent == []
