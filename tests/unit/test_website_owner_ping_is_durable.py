"""One website handoff reaches each owner once, across restarts and workers.

The only guard used to be `SiteSession.pinged`, an in-process flag. It dies with the
task and two workers hold separate copies, so a deploy mid-conversation — or simply a
second worker — could tell Assaf about the same visitor twice. The durable claim
ledger decides now; the flag is only a hint that saves a round trip.

The other half matters just as much: a ping that genuinely failed to send must give
its claim back, or a lead goes permanently unannounced.
"""

from __future__ import annotations

import asyncio

from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.owner_notification_delivery import (
    KIND_WEBSITE_HANDOFF_DELIVERY,
    website_ping_scope,
)
from app.integrations.telegram import TelegramSendError
from app.surfaces.identity import CapturedFields
from app.surfaces.site import (
    SiteSession,
    dump_site_session,
    load_site_session,
    ping_assaf_async,
    reset_site_book,
)


class RecordingPort:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message) -> None:
        self.sent.append(message.conversation_id)


class ExplodingPort:
    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, message) -> None:
        self.attempts += 1
        raise TelegramSendError("Telegram sendMessage failed: HTTP 500")


def _settings() -> Settings:
    return Settings(_env_file=None, telegram_owner_user_ids="12345,67890")


def _ready_session(session_id: str) -> SiteSession:
    session = SiteSession(session_id=session_id)
    session.fields = CapturedFields(name="דנה", phone="050-0000000")
    session.turns = [("visitor", "צריכה אתר"), ("mia", "בסדר")]
    return session


def _ledger(store: LeadStore, db, session_id: str):
    lead_id, notification_key = website_ping_scope(session_id)

    def claim(recipient_id: str) -> bool:
        won = store.try_claim_owner_notification_recipient_compatible(
            kind=KIND_WEBSITE_HANDOFF_DELIVERY,
            compatible_kinds=(KIND_WEBSITE_HANDOFF_DELIVERY,),
            lead_id=lead_id,
            notification_key=notification_key,
            recipient_id=recipient_id,
            claimed_at="2026-09-05T10:00:00+00:00",
        )
        db.commit()
        return won

    def release(recipient_id: str) -> None:
        store.release_owner_notification_recipient_claim(
            kind=KIND_WEBSITE_HANDOFF_DELIVERY,
            lead_id=lead_id,
            notification_key=notification_key,
            recipient_id=recipient_id,
        )
        db.commit()

    return claim, release


def test_a_second_ping_for_the_same_session_reaches_nobody_twice() -> None:
    init_db()
    reset_site_book()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        claim, release = _ledger(store, db, "web_dur_1")
        port = RecordingPort()

        first = asyncio.run(
            ping_assaf_async(
                _settings(), port, _ready_session("web_dur_1"), claim=claim, release=release
            )
        )
        assert first is True
        assert sorted(port.sent) == ["12345", "67890"]

        # A restart loses `pinged`. A brand new SiteSession object, same conversation.
        second = asyncio.run(
            ping_assaf_async(
                _settings(), port, _ready_session("web_dur_1"), claim=claim, release=release
            )
        )
        # Reported as handled so the caller stops trying, but nothing was sent again.
        assert second is True
        assert sorted(port.sent) == ["12345", "67890"]
    finally:
        db.close()


def test_a_failed_delivery_gives_the_claim_back_so_a_retry_still_reaches_assaf() -> None:
    init_db()
    reset_site_book()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        claim, release = _ledger(store, db, "web_dur_2")

        exploding = ExplodingPort()
        failed = asyncio.run(
            ping_assaf_async(
                _settings(),
                exploding,
                _ready_session("web_dur_2"),
                claim=claim,
                release=release,
            )
        )
        assert failed is False
        assert exploding.attempts == 2

        # Telegram recovers. The lead must not stay unannounced.
        working = RecordingPort()
        retried = asyncio.run(
            ping_assaf_async(
                _settings(),
                working,
                _ready_session("web_dur_2"),
                claim=claim,
                release=release,
            )
        )
        assert retried is True
        assert sorted(working.sent) == ["12345", "67890"]
    finally:
        db.close()


def test_finalized_survives_a_restart() -> None:
    """A repeated /end after a deploy must not re-run finalization."""
    session = _ready_session("web_dur_3")
    session.finalized = True
    raw = dump_site_session(session)

    rehydrated = SiteSession(session_id="web_dur_3")
    assert load_site_session(rehydrated, raw) is True
    assert rehydrated.finalized is True
