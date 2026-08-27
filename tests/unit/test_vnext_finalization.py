"""Website finalization: the owner gets exactly one useful ping per conversation.

This file used to carry an in-memory `_MemStore` whose `try_insert_owner_notification`
re-implemented the very `(kind, lead_id)` key it was supposed to be validating. The fake
agreed with the bug, so the suite went green while a returning lead's second conversation
was being classified as a duplicate and never reported. That fake is gone.

The rule here now: exercise the real `LeadStore` against the real test database, and fake
only the outbound Telegram HTTP call — the one thing a unit test genuinely cannot do. The
recording client records; it decides nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from app.core.config import Settings
from app.db.base import Base
from app.db.models import ChannelIdentityRow
from app.db.session import get_session_factory, init_db, make_engine
from app.db.store import LeadStore
from app.domain import hot_handoff as hot_handoff_mod
from app.domain.events import Channel, build_message_in_event, build_message_out_event
from app.domain.hot_handoff import KIND_HOT_LEAD, apply_hot_handoff
from app.domain.sales import FitLevel, PainLevel
from app.main import app
from app.services import notifications as notifications_mod
from app.services.finalization import (
    KIND,
    ConversationSummary,
    build_conversation_summary,
    finalize_website_conversation,
    kind_for,
    qualify_and_finalize,
    scan_inactive_website_conversations,
)
from app.services.notifications import render_conversation_summary
from fastapi.testclient import TestClient
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker

# The unit DB is one shared in-memory sqlite for the whole module, so conversations
# seeded here must sit AFTER the inactivity cutoffs the scan tests below use. Otherwise
# this file's own fixtures show up as inactive conversations in those counts.
_START = datetime(2026, 9, 10, 9, 0, tzinfo=UTC)


class _RecordingTelegram:
    """Fake `httpx.Client` for the owner send. Records; never suppresses."""

    def __init__(self) -> None:
        self.sends: list[dict[str, object]] = []

    def __enter__(self) -> _RecordingTelegram:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
        self.sends.append(json)
        return httpx.Response(200, json={"ok": True})

    def texts(self) -> list[str]:
        return [str(send.get("text", "")) for send in self.sends]


def _patch_owner_send(monkeypatch, module=notifications_mod) -> _RecordingTelegram:
    client = _RecordingTelegram()
    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: client)
    return client


def _owner_settings(**overrides) -> Settings:
    return Settings(
        telegram_bot_token="tok", telegram_owner_user_ids="111", **overrides
    )


def _seed_turns(
    store: LeadStore,
    *,
    session_id: str,
    turns: list[tuple[str, str]],
    lead_id: str | None = None,
    start: datetime = _START,
) -> str:
    """Real canonical message rows, the same ones the website writes."""
    if lead_id is None:
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=session_id
        )
    for index, (role, text) in enumerate(turns):
        occurred_at = start + timedelta(seconds=index)
        provider_event_id = f"in.{session_id}.{index}"
        if role == "prospect":
            built = build_message_in_event(
                provider="website",
                channel=Channel.WEBSITE,
                provider_event_id=provider_event_id,
                conversation_id=session_id,
                text=text,
                actor_role="prospect",
                lead_id=lead_id,
                occurred_at=occurred_at,
            )
        else:
            built = build_message_out_event(
                provider="website",
                channel=Channel.WEBSITE,
                inbound_provider_event_id=provider_event_id,
                conversation_id=session_id,
                text=text,
                lead_id=lead_id,
                occurred_at=occurred_at,
            )
        store.save_canonical_event(provider="website", event=built)
    return lead_id


def _rendered(summary: ConversationSummary) -> str:
    payload = summary.model_dump()
    return render_conversation_summary(
        {key: value if isinstance(value, str) else None for key, value in payload.items()}
    )


def _label_lines(text: str) -> list[str]:
    return [line.split(":", 1)[0] for line in text.splitlines() if ":" in line]


def test_summary_omits_empty_fields() -> None:
    text = render_conversation_summary(
        {
            "name": "Dana",
            "budget": None,
            "conversation_id": "web_1",
            "lead_id": "lead_omit12abcd",
        }
    )
    assert "Dana" in text
    assert "תקציב" not in text
    assert "web_1" in text
    assert "שיחה" in text


def test_two_conversations_for_one_lead_both_claim_and_both_ping(monkeypatch) -> None:
    """Defect 1. The claim is per conversation, so a returning lead is not lost.

    Keyed on (kind, lead_id) the second conversation came back duplicate=True and the
    owner was never told a returning customer had come back and talked again.
    """
    telegram = _patch_owner_send(monkeypatch)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        first_id = "web_return_one01"
        second_id = "web_return_two02"
        lead_id = _seed_turns(
            store,
            session_id=first_id,
            turns=[("prospect", "היי, יש לי חנות תכשיטים")],
        )
        _seed_turns(
            store,
            session_id=second_id,
            turns=[("prospect", "חזרתי, רוצה להתקדם")],
            lead_id=lead_id,
            start=_START + timedelta(days=7),
        )
        db.commit()
        settings = _owner_settings()

        first = qualify_and_finalize(
            store,
            session_id=first_id,
            lead_id=lead_id,
            settings=settings,
            next_step="session_closed",
            require_visitor_message=True,
        )
        db.commit()
        second = qualify_and_finalize(
            store,
            session_id=second_id,
            lead_id=lead_id,
            settings=settings,
            next_step="session_closed",
            require_visitor_message=True,
        )
        db.commit()

        assert first is not None and second is not None
        assert (first.claimed, first.duplicate, first.sent) == (True, False, True)
        assert (second.claimed, second.duplicate, second.sent) == (True, False, True)
        assert len(telegram.sends) == 2
        assert first_id in telegram.texts()[0]
        assert second_id in telegram.texts()[1]
    finally:
        db.close()


def test_same_conversation_finalized_twice_pings_once(monkeypatch) -> None:
    telegram = _patch_owner_send(monkeypatch)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_once01abcdef"
        lead_id = _seed_turns(
            store,
            session_id=session_id,
            turns=[("prospect", "שלום, רוצה לשמוע עוד")],
        )
        db.commit()
        settings = _owner_settings()
        summary = ConversationSummary(
            conversation_id=session_id, lead_id=lead_id, recommended_next_step="call"
        )

        first = finalize_website_conversation(store, summary=summary, settings=settings)
        db.commit()
        second = finalize_website_conversation(store, summary=summary, settings=settings)
        db.commit()

        assert (first.claimed, first.sent, first.duplicate) == (True, True, False)
        assert (second.claimed, second.sent, second.duplicate) == (False, False, True)
        assert first.kind == kind_for("v1")
        assert len(telegram.sends) == 1
        assert store.has_owner_notification_claim(
            kind=KIND, lead_id=lead_id, conversation_id=session_id
        )
        assert not store.has_owner_notification_claim(
            kind=KIND, lead_id=lead_id, conversation_id="some_other_session"
        )
    finally:
        db.close()


def test_concurrent_duplicate_claim_returns_false_and_never_raises(tmp_path) -> None:
    """Defect 2. A racing writer must lose the claim, not blow up the request.

    The interleaving is forced rather than hoped for: a `before_cursor_execute` hook fires
    once, immediately before this session's claiming INSERT reaches the driver, and lets a
    second connection insert and commit the very same claim first. A read-then-write claim
    has already done its SELECT by that point and walks into an IntegrityError that
    propagates out of `POST /v1/website/sessions/{id}/end`. A single
    `INSERT ... ON CONFLICT DO NOTHING` simply reports that it inserted nothing.
    """
    engine = make_engine(f"sqlite:///{tmp_path / 'race.db'}")
    try:
        Base.metadata.create_all(bind=engine)
        factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        loser = factory()
        racer = factory()
        claim = {
            "kind": KIND,
            "lead_id": "lead_race000001",
            "conversation_id": "web_race00000001",
        }
        fired: list[str] = []

        @event.listens_for(engine, "before_cursor_execute")
        def _let_the_racer_in(conn, cursor, statement, parameters, context, many):
            del conn, cursor, parameters, context, many
            if fired or "owner_notification" not in statement.lower():
                return
            if not statement.lstrip().upper().startswith("INSERT"):
                return
            fired.append(statement)
            LeadStore(racer).try_claim_owner_notification(
                **claim, claimed_at="2026-08-26T09:00:00+00:00"
            )
            racer.commit()

        try:
            claimed = LeadStore(loser).try_insert_owner_notification(
                kind=claim["kind"],
                lead_id=claim["lead_id"],
                conversation_id=claim["conversation_id"],
                scheduled_at="2026-08-26T09:00:01+00:00",
            )
            loser.commit()
        finally:
            event.remove(engine, "before_cursor_execute", _let_the_racer_in)

        assert fired, "the racing writer never got in — the test proved nothing"
        assert claimed is False
        assert LeadStore(loser).has_owner_notification_claim(**claim) is True
        loser.close()
        racer.close()
    finally:
        engine.dispose()


def test_apply_hot_handoff_twice_sends_one_brief(monkeypatch) -> None:
    """Defect 3. The persist call returned None, so every retry re-sent the same brief."""
    telegram = _patch_owner_send(monkeypatch, module=hot_handoff_mod)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_hot01abcdefg"
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=session_id
        )
        db.commit()
        settings = _owner_settings()

        for _ in range(2):
            apply_hot_handoff(
                store,
                lead_id=lead_id,
                inbound_id="in_hot_1",
                want="רוצה לדבר עכשיו",
                kill_switch=False,
                settings=settings,
            )
            db.commit()

        assert len(telegram.sends) == 1
        assert lead_id in str(telegram.sends[0]["text"])
        assert store.has_owner_notification(kind=KIND_HOT_LEAD, lead_id=lead_id)
    finally:
        db.close()


def test_summary_carries_the_facts_the_conversation_actually_produced() -> None:
    """Gap 4. Everything below is read from state we already hold — no LLM, no invention."""
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_facts01abcde"
        lead_id = _seed_turns(
            store,
            session_id=session_id,
            turns=[
                ("prospect", "היי, קוראים לי דנה ויש לי חנות תכשיטים"),
                ("mia", "מה התהליך שהכי תוקע אותך?"),
                ("prospect", "אני צריכה לעדכן מלאי ידנית באקסל כל בוקר"),
                ("mia", "כמה זמן זה לוקח?"),
                ("prospect", "שעתיים ביום, ואני רוצה להתחיל עם זה החודש"),
                ("mia", "יש לך תקציב בראש?"),
                ("prospect", 'התקציב שלי הוא 3000 ש"ח לחודש'),
                ("prospect", "אפשר לחזור אלי ל dana@example.com"),
            ],
        )
        sales = store.get_sales(lead_id)
        sales.fit = FitLevel.GOOD
        sales.pain_level = PainLevel.P3
        sales.workflow_known = True
        sales.manual_step_known = True
        sales.impact_confirmed = True
        sales.timeline_known = True
        sales.meeting_exit_offered = True
        store.save_sales(sales)
        db.commit()

        summary = build_conversation_summary(
            store, session_id=session_id, lead_id=lead_id, next_step="session_closed"
        )
        text = _rendered(summary)

        assert summary.name == "דנה"
        assert summary.contact == "dana@example.com"
        assert summary.business
        assert summary.need is not None and "מלאי" in summary.need
        assert summary.pain is not None and "P3" in summary.pain
        assert summary.relevant_service is not None
        assert "inventory automation" in summary.relevant_service
        assert "spreadsheet automation" in summary.relevant_service
        assert summary.timeline == "this month"
        assert summary.budget is not None and "3000" in summary.budget
        assert summary.qualification == "good"
        assert summary.meeting_status == "offered, not booked"
        assert summary.recommended_next_step == "session_closed"

        labels = _label_lines(text)
        assert len(labels) >= 6, text
        joined = "\n".join(labels)
        for label in (
            "שם",
            "יצירת קשר",
            "צורך",
            "בעיה",
            "שירות",
            "לוח זמנים",
            "תקציב",
            "כישור",
            "פגישה",
            "ליד",
            "וואטסאפ הוצע",
        ):
            assert label in joined, text
    finally:
        db.close()


def test_summary_leaves_undiscussed_facts_out_rather_than_guessing() -> None:
    """The other half of Gap 4: silence stays silence."""
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_quiet01abcde"
        lead_id = _seed_turns(
            store,
            session_id=session_id,
            turns=[("prospect", "היי")],
        )
        db.commit()

        summary = build_conversation_summary(
            store, session_id=session_id, lead_id=lead_id, next_step="session_closed"
        )
        text = _rendered(summary)

        assert summary.budget is None
        assert summary.timeline is None
        assert summary.contact is None
        assert summary.meeting_status is None
        assert summary.qualification is None
        assert "תקציב" not in text
        assert "לוח זמנים" not in text
        assert "יצירת קשר" not in text
        assert "כישור" not in text
    finally:
        db.close()


def test_website_session_end_route_pings_the_owner_exactly_once(monkeypatch) -> None:
    """Gap 5. Drives the real route, so nothing between HTTP and Telegram is stubbed."""
    telegram = _patch_owner_send(monkeypatch)
    settings = _owner_settings()
    monkeypatch.setattr("app.api.website.get_settings", lambda: settings)
    init_db()
    session_id = "web_route01abcde"
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _seed_turns(
            store,
            session_id=session_id,
            turns=[
                ("prospect", "היי, קוראים לי יוסי ואני מוכר שעונים"),
                ("mia", "מה הכי תוקע?"),
                ("prospect", "אני צריך לעדכן מלאי ידנית כל יום"),
            ],
        )
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        response = client.post(f"/v1/website/sessions/{session_id}/end")

    assert response.status_code == 200
    assert response.json()["finalized"] is True
    assert len(telegram.sends) == 1
    sent = telegram.texts()[0]
    assert session_id in sent
    assert "שיחה מהאתר הסתיימה" in sent
    assert "יוסי" in sent
    assert "ליד" in sent
    assert "וואטסאפ הוצע" in sent
    assert telegram.sends[0].get("parse_mode") == "HTML"


def test_website_session_end_route_is_idempotent(monkeypatch) -> None:
    telegram = _patch_owner_send(monkeypatch)
    settings = _owner_settings()
    monkeypatch.setattr("app.api.website.get_settings", lambda: settings)
    init_db()
    session_id = "web_route02abcde"
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _seed_turns(
            store, session_id=session_id, turns=[("prospect", "היי, רוצה לשמוע")]
        )
        db.commit()
    finally:
        db.close()

    with TestClient(app) as client:
        first = client.post(f"/v1/website/sessions/{session_id}/end")
        second = client.post(f"/v1/website/sessions/{session_id}/end")

    assert first.json()["finalized"] is True
    assert second.json()["finalized"] is False
    assert len(telegram.sends) == 1


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


def test_inactivity_scan_still_returns_a_returning_leads_new_conversation() -> None:
    """The scan skipped by lead too, so Defect 1 also hid conversations from the worker."""
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        first_id = "web_scanback01aa"
        second_id = "web_scanback02bb"
        customer_id, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=first_id
        )
        # Same visitor, new session: one customer, one lead, two website identities.
        db.add(
            ChannelIdentityRow(
                customer_id=customer_id,
                channel=Channel.WEBSITE.value,
                external_id=second_id,
                verified=False,
            )
        )
        old = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        for index, session_id in enumerate((first_id, second_id)):
            store.save_canonical_event(
                provider="website",
                event=build_message_in_event(
                    provider="website",
                    channel=Channel.WEBSITE,
                    provider_event_id=f"in.scanback.{index}",
                    conversation_id=session_id,
                    text="שלום",
                    actor_role="prospect",
                    lead_id=lead_id,
                    occurred_at=old + timedelta(seconds=index),
                ),
            )
        db.commit()
        cutoff = (old + timedelta(minutes=31)).isoformat()

        store.try_insert_owner_notification(
            kind=KIND,
            lead_id=lead_id,
            conversation_id=first_id,
            scheduled_at=old.isoformat(),
        )
        db.commit()

        remaining = store.list_inactive_website_conversations(
            cutoff_iso=cutoff,
            skip_conversation_kinds=(KIND,),
            limit=50,
        )
        sessions = {session_id for session_id, _ in remaining}
        assert first_id not in sessions
        assert second_id in sessions
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
