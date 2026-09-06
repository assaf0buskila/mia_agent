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
from types import SimpleNamespace

from app.api.website import (
    _delivery_accepted,
    _maybe_ping_owner,
    create_handoff,
    end_session,
    process_website_message,
)
from app.core.config import Settings
from app.db.models import OwnerNotificationRecipientClaimRow, WebsiteSessionStateRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.handoff.delivery import (
    KIND_WEBSITE_HANDOFF_DELIVERY,
    website_ping_scope,
)
from app.domain.tools import AdapterHttpError
from app.integrations.sheets import FakeSheetsPort
from app.integrations.telegram import TelegramSendError
from app.surfaces.identity import CapturedFields
from app.surfaces.site import (
    SiteSession,
    dump_site_session,
    format_owner_ping,
    load_site_session,
    ping_assaf_async,
    reset_site_book,
    site_book,
)
from sqlalchemy import select


class RecordingPort:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message) -> None:
        self.sent.append(message.conversation_id)


class BriefRecordingPort:
    def __init__(self) -> None:
        self.sent = []

    async def send(self, message) -> None:
        self.sent.append(message)


def test_claim_insert_uses_returned_key_when_driver_rowcount_is_unknown() -> None:
    class ReturnedResult:
        rowcount = -1

        def __init__(self, row) -> None:
            self._row = row

        def first(self):
            return self._row

    class ClaimSession:
        def __init__(self, row) -> None:
            self.row = row
            self.statement = None

        def get_bind(self):
            return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

        def execute(self, statement):
            self.statement = statement
            return ReturnedResult(self.row)

    values = {
        "kind": "website_handoff_delivery",
        "lead_id": "website_session:test",
        "notification_key": "test",
        "recipient_id": "12345",
        "claimed_at": "2026-09-06T12:00:00+00:00",
    }
    inserted_session = ClaimSession((values["kind"], values["lead_id"], "test", "12345"))
    inserted = LeadStore(inserted_session)._insert_ignoring_conflicts(
        OwnerNotificationRecipientClaimRow.__table__, values
    )
    assert inserted is True
    assert "RETURNING" in str(inserted_session.statement)

    conflict_session = ClaimSession(None)
    conflict = LeadStore(conflict_session)._insert_ignoring_conflicts(
        OwnerNotificationRecipientClaimRow.__table__, values
    )
    assert conflict is False


class ExplodingPort:
    def __init__(self) -> None:
        self.attempts = 0

    async def send(self, message) -> None:
        self.attempts += 1
        try:
            raise AdapterHttpError(500)
        except AdapterHttpError as exc:
            raise TelegramSendError("Telegram sendMessage failed: HTTP 500") from exc


def _settings() -> Settings:
    return Settings(_env_file=None, telegram_owner_user_ids="12345,67890")


def _ready_session(session_id: str) -> SiteSession:
    session = SiteSession(session_id=session_id)
    session.fields = CapturedFields(name="דנה", phone="050-0000000")
    session.turns = [("visitor", "צריכה אתר"), ("mia", "בסדר")]
    return session


def _commit_newer_contact_over_stale_cache(session_id: str) -> None:
    stale = SiteSession(session_id=session_id)
    stale.turns = [("visitor", "אני רוצה לדבר עם אסף")]
    live = site_book().open(session_id)
    load_site_session(live, dump_site_session(stale))
    with get_session_factory()() as seed_db:
        seed_store = LeadStore(seed_db)
        seed_store.open_website_session(session_id)
        seed_store.save_website_session_state(session_id, dump_site_session(stale))
        seed_db.commit()
    with get_session_factory()() as newer_db:
        newer_store = LeadStore(newer_db)
        newer = SiteSession(session_id=session_id)
        assert load_site_session(newer, newer_store.load_website_session_state(session_id))
        newer.fields = CapturedFields(
            name="דנה",
            email="dana@example.invalid",
            business="סטודיו עדכני",
            want="לידים נופלים בערב",
        )
        newer.confirmed = True
        newer.business_known = True
        newer.friction_known = True
        newer.business_summary = "סטודיו עדכני"
        newer.friction_summary = "לידים נופלים בערב"
        newer.turns.append(("visitor", "האימייל שלי dana@example.invalid"))
        newer_store.save_website_session_state(session_id, dump_site_session(newer))
        newer_db.commit()


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
        # A claim alone is not delivery evidence. The caller must consult the durable
        # accepted receipt before telling the visitor that Assaf received anything.
        assert second is False
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
        assert exploding.attempts == 4  # two owners, one bounded retry each

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


def test_the_crash_window_between_a_good_send_and_the_saved_flag() -> None:
    """The worst case: the ping lands, then the process dies before it is recorded.

    Sequence: claim -> Telegram accepts -> the task is killed before `session.pinged`
    is set or the session row is written. A new process rehydrates a session that
    still says pinged=False, so the in-memory guard lets it through. Only the durable
    claim stands between Assaf and a second identical ping about the same visitor.

    This uses a genuinely separate DB session for the second attempt, so it proves the
    claim survives a process boundary rather than an object boundary.
    """
    init_db()
    reset_site_book()

    # --- process 1: claims, sends, then dies -------------------------------
    first_db = get_session_factory()()
    try:
        claim, release = _ledger(LeadStore(first_db), first_db, "web_crash")
        port = RecordingPort()
        sent = asyncio.run(
            ping_assaf_async(
                _settings(), port, _ready_session("web_crash"), claim=claim, release=release
            )
        )
        assert sent is True
        assert sorted(port.sent) == ["12345", "67890"]
    finally:
        # No `session.pinged = True` is ever persisted: that is the crash.
        first_db.close()

    # --- process 2: cold start, same conversation --------------------------
    second_db = get_session_factory()()
    try:
        claim, release = _ledger(LeadStore(second_db), second_db, "web_crash")
        revived = _ready_session("web_crash")
        assert revived.pinged is False  # the flag genuinely did not survive
        port_after_restart = RecordingPort()
        again = asyncio.run(
            ping_assaf_async(
                _settings(),
                port_after_restart,
                revived,
                claim=claim,
                release=release,
            )
        )
        # The retained claim prevents a duplicate, but is not called a delivery.
        assert again is False
        assert port_after_restart.sent == []
    finally:
        second_db.close()


def test_finalized_survives_a_restart() -> None:
    """A repeated /end after a deploy must not re-run finalization."""
    session = _ready_session("web_dur_3")
    session.finalized = True
    raw = dump_site_session(session)

    rehydrated = SiteSession(session_id="web_dur_3")
    assert load_site_session(rehydrated, raw) is True
    assert rehydrated.finalized is True


def test_owner_brief_keeps_context_and_skips_contact_only_quote() -> None:
    session = _ready_session("web_context_brief")
    session.fields = CapturedFields(
        name="דנה",
        phone="050-0000000",
        email="dana@example.com",
        business="סטודיו לציפורניים",
        want="תורים בוואטסאפ מתפספסים",
    )
    session.business_known = True
    session.friction_known = True
    session.business_summary = "סטודיו לציפורניים"
    session.friction_summary = "תורים בוואטסאפ מתפספסים"
    session.turns = [
        ("visitor", "אני מנהלת סטודיו לציפורניים"),
        ("mia", "מה מפריע היום?"),
        ("visitor", "תורים בוואטסאפ מתפספסים"),
        ("visitor", "אפשר לחבר את זה ליומן?"),
        ("mia", "אפשר לבחון חיבור לפי המערכת הקיימת."),
        ("visitor", "050-0000000"),
    ]

    brief = format_owner_ping(session)

    assert "שם: דנה" in brief
    assert "טלפון: 050-0000000" in brief
    assert "אימייל: dana@example.com" in brief
    assert "עסק: סטודיו לציפורניים" in brief
    assert "מה מפריע: תורים בוואטסאפ מתפספסים" in brief
    assert "על מה דיברו:" in brief
    assert "מיה: אפשר לבחון חיבור לפי המערכת הקיימת." in brief
    assert "שאלות שהלקוח העלה: אפשר לחבר את זה ליומן?" in brief
    assert "להשלמה עם אסף: לא ידוע על פרט חסר בשאלות ההיכרות" in brief
    assert "במילים שלהם: אפשר לחבר את זה ליומן?" in brief
    assert "במילים שלהם: 050-0000000" not in brief
    assert "המלצה:" in brief


def test_concurrent_background_and_handoff_share_one_delivery_claim() -> None:
    init_db()
    reset_site_book()
    session_id = "web_concurrent_contact_handoff"
    session = _ready_session(session_id)
    session.confirmed = True
    session.awaiting_ping = True
    from app.surfaces.site import site_book

    live = site_book().open(session_id)
    load_site_session(live, dump_site_session(session))
    with get_session_factory()() as db:
        store = LeadStore(db)
        store.open_website_session(session_id)
        store.save_website_session_state(session_id, dump_site_session(live))
        db.commit()

    class DelayedPort:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send(self, message) -> None:
            self.sent.append(message.conversation_id)
            await asyncio.sleep(0.01)

    port = DelayedPort()
    settings = Settings(_env_file=None, telegram_owner_user_ids="12345")

    async def race():
        return await asyncio.gather(
            _maybe_ping_owner(session_id=session_id, settings=settings, owner_port=port),
            _maybe_ping_owner(
                session_id=session_id, settings=settings, owner_port=port, force=True
            ),
        )

    first_results = asyncio.run(race())
    replay = asyncio.run(
        _maybe_ping_owner(session_id=session_id, settings=settings, owner_port=port, force=True)
    )

    assert port.sent == ["12345"]
    assert sum(bool(result.delivered) for result in first_results) == 1
    assert replay.delivered == ("12345",)


def test_delayed_owner_send_merges_flags_without_erasing_a_newer_turn() -> None:
    init_db()
    reset_site_book()
    session_id = "web_delayed_ping_state_race"
    stale = _ready_session(session_id)
    stale.confirmed = True
    stale.awaiting_ping = True
    stale.business_summary = "סטודיו ישן"
    stale.friction_summary = "בעיה ישנה"
    from app.surfaces.site import site_book

    live = site_book().open(session_id)
    load_site_session(live, dump_site_session(stale))
    with get_session_factory()() as db:
        store = LeadStore(db)
        store.open_website_session(session_id)
        store.save_website_session_state(session_id, dump_site_session(stale))
        db.commit()

    send_started = asyncio.Event()
    allow_receipt = asyncio.Event()

    class DelayedReceiptPort:
        async def send(self, _message) -> None:
            send_started.set()
            await allow_receipt.wait()

    settings = Settings(_env_file=None, telegram_owner_user_ids="12345")

    async def race() -> None:
        ping = asyncio.create_task(
            _maybe_ping_owner(
                session_id=session_id,
                settings=settings,
                owner_port=DelayedReceiptPort(),
            )
        )
        await send_started.wait()
        # This is a different DB connection, like another worker handling the next
        # visitor turn while Telegram delivery is in flight.
        with get_session_factory()() as newer_db:
            newer_store = LeadStore(newer_db)
            raw = newer_store.load_website_session_state(session_id)
            newer = SiteSession(session_id=session_id)
            assert load_site_session(newer, raw)
            newer.business_summary = "סטודיו חדש"
            newer.friction_summary = "לידים נופלים אחרי שעות הפעילות"
            newer.turns.append(("visitor", "הבעיה החדשה היא אחרי שעות הפעילות"))
            newer_store.save_website_session_state(session_id, dump_site_session(newer))
            newer_db.commit()
        allow_receipt.set()
        delivery = await ping
        assert delivery.delivered == ("12345",)

    asyncio.run(race())

    with get_session_factory()() as verify_db:
        raw = LeadStore(verify_db).load_website_session_state(session_id)
    persisted = SiteSession(session_id=session_id)
    assert load_site_session(persisted, raw)
    assert persisted.pinged is True
    assert persisted.business_summary == "סטודיו חדש"
    assert persisted.friction_summary == "לידים נופלים אחרי שעות הפעילות"
    assert persisted.turns[-1] == (
        "visitor",
        "הבעיה החדשה היא אחרי שעות הפעילות",
    )


def test_flag_merge_refreshes_a_cached_row_before_writing() -> None:
    init_db()
    session_id = "web_cached_row_flag_merge"
    original = _ready_session(session_id)
    with get_session_factory()() as seed_db:
        seed_store = LeadStore(seed_db)
        seed_store.open_website_session(session_id)
        seed_store.save_website_session_state(session_id, dump_site_session(original))
        seed_db.commit()

    cached_db = get_session_factory()()
    try:
        cached_row = cached_db.scalar(
            select(WebsiteSessionStateRow).where(WebsiteSessionStateRow.session_id == session_id)
        )
        assert cached_row is not None

        with get_session_factory()() as newer_db:
            newer_store = LeadStore(newer_db)
            newer = SiteSession(session_id=session_id)
            assert load_site_session(newer, newer_store.load_website_session_state(session_id))
            newer.pinged = True
            newer.business_summary = "העסק המעודכן"
            newer.turns.append(("visitor", "הודעה חדשה אחרי טעינת השורה"))
            newer_store.save_website_session_state(session_id, dump_site_session(newer))
            newer_db.commit()

        LeadStore(cached_db).merge_website_session_notification_flags(
            session_id, finalized=True, crm_written=True
        )
        cached_db.commit()
    finally:
        cached_db.close()

    with get_session_factory()() as verify_db:
        raw = LeadStore(verify_db).load_website_session_state(session_id)
    persisted = SiteSession(session_id=session_id)
    assert load_site_session(persisted, raw)
    assert persisted.finalized is True
    assert persisted.crm_written is True
    assert persisted.pinged is True
    assert persisted.business_summary == "העסק המעודכן"
    assert persisted.turns[-1] == ("visitor", "הודעה חדשה אחרי טעינת השורה")


def test_flag_merge_does_not_replace_malformed_session_state() -> None:
    init_db()
    session_id = "web_malformed_flag_merge"
    with get_session_factory()() as db:
        store = LeadStore(db)
        store.open_website_session(session_id)
        store.save_website_session_state(
            session_id, dump_site_session(SiteSession(session_id=session_id))
        )
        row = db.scalar(
            select(WebsiteSessionStateRow).where(WebsiteSessionStateRow.session_id == session_id)
        )
        assert row is not None
        row.state_json = "{malformed"
        db.commit()
        assert store.merge_website_session_notification_flags(session_id, pinged=True) == ""
        db.commit()
    with get_session_factory()() as verify_db:
        row = verify_db.scalar(
            select(WebsiteSessionStateRow).where(WebsiteSessionStateRow.session_id == session_id)
        )
        assert row is not None
        assert row.state_json == "{malformed"


def test_stale_full_save_preserves_background_monotonic_flags_and_new_turn() -> None:
    init_db()
    session_id = "web_stale_full_save_flags"
    initial = _ready_session(session_id)
    with get_session_factory()() as seed_db:
        seed_store = LeadStore(seed_db)
        seed_store.open_website_session(session_id)
        seed_store.save_website_session_state(session_id, dump_site_session(initial))
        seed_db.commit()

    foreground_db = get_session_factory()()
    try:
        foreground_store = LeadStore(foreground_db)
        stale_raw = foreground_store.load_website_session_state(session_id)
        stale = SiteSession(session_id=session_id)
        assert load_site_session(stale, stale_raw)

        with get_session_factory()() as background_db:
            background_store = LeadStore(background_db)
            background_store.merge_website_session_notification_flags(
                session_id,
                pinged=True,
                finalized=True,
                crm_written=True,
            )
            background_db.commit()

        stale.business_summary = "foreground newer conversation"
        stale.turns.append(("visitor", "new foreground turn"))
        foreground_store.save_website_session_state(session_id, dump_site_session(stale))
        foreground_db.commit()
    finally:
        foreground_db.close()

    with get_session_factory()() as verify_db:
        persisted = SiteSession(session_id=session_id)
        assert load_site_session(
            persisted, LeadStore(verify_db).load_website_session_state(session_id)
        )
    assert persisted.pinged is True
    assert persisted.finalized is True
    assert persisted.crm_written is True
    assert persisted.business_summary == "foreground newer conversation"
    assert persisted.turns[-1] == ("visitor", "new foreground turn")


def test_handoff_uses_newer_durable_contact_and_brief(monkeypatch) -> None:
    init_db()
    reset_site_book()
    session_id = "web_stale_cache_handoff"
    _commit_newer_contact_over_stale_cache(session_id)
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", "12345")
    port = BriefRecordingPort()
    request_db = get_session_factory()()
    try:
        response = asyncio.run(create_handoff(session_id, request_db, port))
    finally:
        request_db.close()
    assert response.notification_status == "delivered"
    assert len(port.sent) == 1
    assert "dana@example.invalid" in port.sent[0].text
    assert "סטודיו עדכני" in port.sent[0].text
    assert "לידים נופלים בערב" in port.sent[0].text


def test_end_uses_newer_durable_contact_and_finalizes(monkeypatch) -> None:
    init_db()
    reset_site_book()
    session_id = "web_stale_cache_end"
    _commit_newer_contact_over_stale_cache(session_id)
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", "12345")
    port = BriefRecordingPort()
    request_db = get_session_factory()()
    try:
        response = asyncio.run(end_session(session_id, request_db, port))
        request_db.commit()
    finally:
        request_db.close()
    assert response.finalized is True
    assert len(port.sent) == 1
    assert "dana@example.invalid" in port.sent[0].text


def test_message_refreshes_newer_committed_state_without_mutating_stale_cache() -> None:
    init_db()
    reset_site_book()
    session_id = "web_stale_cache_message"
    _commit_newer_contact_over_stale_cache(session_id)
    stale = site_book().get(session_id)
    assert stale is not None and not stale.fields.has_phone_or_email()
    with get_session_factory()() as request_db:
        process_website_message(
            LeadStore(request_db),
            session_id=session_id,
            text="תודה",
            settings=Settings(_env_file=None),
            sheets=FakeSheetsPort(),
        )
        request_db.commit()
    with get_session_factory()() as verify_db:
        refreshed = SiteSession(session_id=session_id)
        assert load_site_session(
            refreshed, LeadStore(verify_db).load_website_session_state(session_id)
        )
    assert refreshed.fields.email == "dana@example.invalid"
    assert refreshed.business_summary == "סטודיו עדכני"
    assert stale.fields.email == ""


def test_partial_fanout_is_not_complete_delivery() -> None:
    from app.services.notifications import OwnerTelegramDelivery

    assert not _delivery_accepted(OwnerTelegramDelivery(delivered=("111",), rejected=("222",)))
    assert not _delivery_accepted(OwnerTelegramDelivery(delivered=("111",), ambiguous=("222",)))
    assert _delivery_accepted(OwnerTelegramDelivery(delivered=("111", "222")))
