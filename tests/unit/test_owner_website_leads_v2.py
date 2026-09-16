"""v2 CRM website captures must be visible to owner reports, not just legacy leads.

Before this fix, `compute_daily_brief`/`format_website_conversations_ack`/
`format_hot_leads_ack` only ever read the legacy `SalesState`/`lead_created` path,
which the real website flow (`app/surfaces/site_v2.py`) never writes to. A v2 capture
was therefore invisible to every owner report.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.capabilities.types import Principal
from app.db.base import Base
from app.db.models import CrmOutboxRow
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.handoff.hot import format_hot_leads_ack
from app.domain.owner.briefs import compute_daily_brief
from app.domain.owner.reads import format_website_conversations_ack
from app.domain.owner.snapshot import format_operator_snapshot_ack
from app.domain.owner.status import format_owner_status_ack
from app.services.crm_v2 import CrmService
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

_TZ = "Asia/Jerusalem"


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False, autoflush=False)
    try:
        yield factory
    finally:
        engine.dispose()


def _capture(session: Session, *, conversation_id: str, now: datetime | None = None) -> None:
    result = CrmService(session, now=now).capture_site_lead(
        {"name": "דנה", "phone": "0501234567", "business": "מספרה", "want": "יותר לקוחות"},
        conversation_id=conversation_id,
        source_ref=f"site:{conversation_id}:m1",
        summary="ביקשה יעוץ",
        recipient_ids=("999",),
    )
    assert result.contact is not None
    session.commit()


def test_v2_capture_counts_once_in_daily_brief(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        store = LeadStore(session)
        before = compute_daily_brief(store, timezone=_TZ)
        assert before is not None
        _capture(session, conversation_id="v2-brief-1")
        after = compute_daily_brief(store, timezone=_TZ)
        assert after is not None
        assert after.leads == before.leads + 1


def test_v2_capture_appears_once_in_website_conversations(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        store = LeadStore(session)
        _capture(session, conversation_id="v2-conv-1")
        ack = format_website_conversations_ack(store)
        assert ack.count("מספרה") == 1
        assert "יעוץ" not in ack  # summary is not the displayed field; want/next_step are


def test_v2_capture_is_included_in_hot_leads_and_status(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        store = LeadStore(session)
        result = CrmService(session).capture_site_lead(
            {"name": "יוסי", "phone": "0507654321", "business": "מוסך"},
            conversation_id="v2-hot-1",
            source_ref="site:v2-hot-1:m1",
            summary="רוצה שיחה",
            recipient_ids=("999",),
        )
        assert result.contact is not None
        session.commit()
        # No Telegram outbox job was ever confirmed for this contact, so it is
        # exactly the "Assaf has not reliably been told" signal `format_hot_leads_ack`
        # now surfaces for v2.
        hot = format_hot_leads_ack(store, principal=Principal.owner(source="test"))
        # C9 (R3): the reply shows the captured name, never the raw crm_... id.
        assert result.contact.id not in hot
        assert "יוסי" in hot

        status = format_owner_status_ack(
            store, principal=Principal.owner(source="test"), timezone=_TZ
        )
        assert "לידים 1" in status

        snapshot = format_operator_snapshot_ack(
            store, principal=Principal.owner(source="test"), timezone=_TZ
        )
        assert result.contact.id not in snapshot
        assert "יוסי" in snapshot


def test_hot_lead_with_no_name_falls_back_to_a_position_placeholder_not_the_id(
    sessions: sessionmaker[Session],
) -> None:
    """C9 (R3): a hot lead with no captured name used to fall back to the bare
    crm_... id; Assaf's explicit call for this surface is a name, never a raw
    internal id, so the fallback is now a position-based placeholder instead.
    """
    with sessions() as session:
        store = LeadStore(session)
        result = CrmService(session).capture_site_lead(
            {"phone": "0507654322", "business": "מוסך"},
            conversation_id="v2-hot-noname",
            source_ref="site:v2-hot-noname:m1",
            summary="רוצה שיחה",
            recipient_ids=("999",),
        )
        assert result.contact is not None
        session.commit()
        hot = format_hot_leads_ack(store, principal=Principal.owner(source="test"))
        assert result.contact.id not in hot
        assert "פנייה 1" in hot


def test_hot_leads_sharing_a_rendered_name_are_never_merged(
    sessions: sessionmaker[Session],
) -> None:
    """C9 (P2 review fix): dedup in format_hot_leads_ack must run on the
    underlying contact id, never on the rendered label. Two different website
    captures that happen to share a first name -- common in Hebrew -- used to
    collapse into one line via `dict.fromkeys` over the label, silently
    telling Assaf he has fewer hot leads than he actually does.
    """
    with sessions() as session:
        store = LeadStore(session)
        service = CrmService(session)
        first = service.capture_site_lead(
            {"name": "דנה", "phone": "0501110001", "business": "עסק א"},
            conversation_id="hot-dup-name-1",
            source_ref="site:hot-dup-name-1:m1",
            summary="x",
            recipient_ids=("999",),
        )
        second = service.capture_site_lead(
            {"name": "דנה", "phone": "0501110002", "business": "עסק ב"},
            conversation_id="hot-dup-name-2",
            source_ref="site:hot-dup-name-2:m1",
            summary="y",
            recipient_ids=("999",),
        )
        assert first.contact is not None and second.contact is not None
        assert first.contact.id != second.contact.id  # two genuinely different leads
        session.commit()

        hot = format_hot_leads_ack(store, principal=Principal.owner(source="test"))
        # Both leads must be counted, not collapsed into a single "דנה".
        assert hot.count("דנה") == 2
        assert hot == "לידים חמים: דנה, דנה"


def test_v1_takeover_state_lead_still_reported_alongside_v2(
    sessions: sessionmaker[Session],
) -> None:
    """2026-09-16 review P0: a `LeadRow` with `takeover_state ==
    HUMAN_TAKEOVER_REQUIRED` (set by `store.set_takeover_state`, historically via
    the now-removed `apply_hot_handoff`) must keep surfacing here even though the
    auto-freeze writer is gone -- production has exactly this row today. Union
    with a v2 capture proves neither source hides the other.
    """
    from app.domain.conversation_scope import TakeoverState

    with sessions() as session:
        store = LeadStore(session)
        _customer_id, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id="972500001111"
        )
        store.set_takeover_state(lead_id, TakeoverState.HUMAN_TAKEOVER_REQUIRED.value)
        v2 = CrmService(session).capture_site_lead(
            {"name": "Dana", "phone": "0501112222", "business": "Studio"},
            conversation_id="v1-union-1",
            source_ref="site:v1-union-1:m1",
            summary="x",
            recipient_ids=("999",),
        )
        assert v2.contact is not None
        session.commit()

        hot = format_hot_leads_ack(store, principal=Principal.owner(source="test"))
        # C9 (R3): neither source's raw id is ever shown; the v1 lead (no
        # headline) gets a position placeholder, the v2 lead shows its name.
        assert lead_id not in hot
        assert v2.contact.id not in hot
        assert "ליד 1" in hot
        assert "Dana" in hot


def test_v1_takeover_state_lead_shows_its_sales_headline_as_a_label(
    sessions: sessionmaker[Session],
) -> None:
    """Mirrors the v2 name-label fix: a v1 lead's `SalesState.headline` (the
    closest thing v1 has to a name -- `LeadRow`/`CustomerRow` store no name at
    all) is used as the label INSTEAD of the id, never alongside it (C9, R3:
    no raw internal id in owner prose).
    """
    from app.domain.conversation_scope import TakeoverState
    from app.domain.sales import SalesState

    with sessions() as session:
        store = LeadStore(session)
        _customer_id, lead_id = store.open_channel_lead(
            channel=Channel.WHATSAPP, external_id="972500002222"
        )
        store.set_takeover_state(lead_id, TakeoverState.HUMAN_TAKEOVER_REQUIRED.value)
        store.save_sales(SalesState(lead_id=lead_id, headline="needs a site urgently"))
        session.commit()

        hot = format_hot_leads_ack(store, principal=Principal.owner(source="test"))
        assert lead_id not in hot
        assert "needs a site urgently" in hot


def test_legacy_lead_still_counted_without_v2(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        store = LeadStore(session)
        before = compute_daily_brief(store, timezone=_TZ)
        assert before is not None
        store.open_channel_lead(channel=Channel.WEBSITE, external_id="legacy-only-1")
        session.commit()
        after = compute_daily_brief(store, timezone=_TZ)
        assert after is not None
        assert after.leads == before.leads + 1


def test_same_conversation_in_legacy_and_v2_counts_once(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        store = LeadStore(session)
        before = compute_daily_brief(store, timezone=_TZ)
        assert before is not None
        shared_conversation_id = "shared-session-dedupe-1"
        store.open_channel_lead(channel=Channel.WEBSITE, external_id=shared_conversation_id)
        _capture(session, conversation_id=shared_conversation_id)
        after = compute_daily_brief(store, timezone=_TZ)
        assert after is not None
        # Both mechanisms fired for the same website session: counted once, not twice.
        assert after.leads == before.leads + 1


def test_v2_capture_before_local_midnight_yesterday_excluded_from_todays_count(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        store = LeadStore(session)
        tz = ZoneInfo(_TZ)
        today_local_noon = datetime(2026, 9, 15, 12, 0, tzinfo=tz)
        yesterday_just_before_midnight = datetime(
            2026, 9, 14, 23, 59, 30, tzinfo=tz
        ).astimezone(UTC)
        _capture(
            session,
            conversation_id="v2-midnight-boundary-1",
            now=yesterday_just_before_midnight,
        )
        today = compute_daily_brief(store, timezone=_TZ, now=today_local_noon)
        assert today is not None
        assert today.leads == 0
        yesterday = compute_daily_brief(
            store,
            timezone=_TZ,
            now=today_local_noon - timedelta(days=1),
        )
        assert yesterday is not None
        assert yesterday.leads == 1


def test_empty_state_strings_unchanged_when_no_leads(sessions: sessionmaker[Session]) -> None:
    with sessions() as session:
        store = LeadStore(session)
        assert format_website_conversations_ack(store) == "אין עדיין שיחות מהאתר לנתח."
        assert (
            format_hot_leads_ack(store, principal=Principal.owner(source="test"))
            == "אין לידים חמים שמחכים לתפיסה."
        )


def test_returning_visitor_second_session_shown_hot_despite_first_confirmed(
    sessions: sessionmaker[Session],
) -> None:
    """Regression: 'undelivered' must be judged per capturing conversation, not per
    contact -- a first session's confirmed Telegram ping must never hide a second
    session's genuinely undelivered one for the same returning visitor.
    """
    with sessions() as session:
        store = LeadStore(session)
        service = CrmService(session)
        first = service.capture_site_lead(
            {"name": "Dana", "phone": "0501112222", "business": "Studio"},
            conversation_id="return-sess-1",
            source_ref="site:return-sess-1:m1",
            summary="x",
            recipient_ids=("999",),
        )
        second = service.capture_site_lead(
            {"name": "Dana", "phone": "0501112222", "want": "more clients"},
            conversation_id="return-sess-2",
            source_ref="site:return-sess-2:m1",
            summary="y",
            recipient_ids=("999",),
        )
        assert first.contact is not None and second.contact is not None
        assert first.contact.id == second.contact.id  # same contact, returning visitor
        session.commit()

        jobs = session.scalars(
            select(CrmOutboxRow).where(CrmOutboxRow.destination == "telegram")
        ).all()
        first_job = next(j for j in jobs if j.dedupe_key.startswith("telegram:crm:return-sess-1:"))
        second_job = next(
            j for j in jobs if j.dedupe_key.startswith("telegram:crm:return-sess-2:")
        )
        first_job.status = "confirmed"
        second_job.status = "failed"
        session.commit()

        hot = format_hot_leads_ack(store, principal=Principal.owner(source="test"))
        # C9 (R3): shown by name, never the raw id.
        assert first.contact.id not in hot
        assert "Dana" in hot


def test_returning_visitor_same_day_counts_as_one_lead_and_one_line(
    sessions: sessionmaker[Session],
) -> None:
    with sessions() as session:
        store = LeadStore(session)
        service = CrmService(session)
        for conversation_id, want in (("dup-sess-1", "w-1"), ("dup-sess-2", "w-2")):
            result = service.capture_site_lead(
                {"name": "Dana", "phone": "0503334444", "business": "Barbershop", "want": want},
                conversation_id=conversation_id,
                source_ref=f"site:{conversation_id}:m1",
                summary="x",
                recipient_ids=("999",),
            )
            assert result.contact is not None
        session.commit()

        brief = compute_daily_brief(store, timezone=_TZ)
        assert brief is not None
        assert brief.leads == 1  # one returning visitor, not two

        ack = format_website_conversations_ack(store)
        assert ack.count("Barbershop") == 1
