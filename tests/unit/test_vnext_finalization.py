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
from app.agents.client.graph import finalize_inactive_website_conversations
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.base import Base
from app.db.models import (
    ChannelIdentityRow,
    OwnerNotificationClaimRow,
    OwnerNotificationRecipientClaimRow,
)
from app.db.session import get_session_factory, init_db, make_engine
from app.db.store import LeadStore
from app.domain import hot_handoff as hot_handoff_module
from app.domain.conversation_scope import TakeoverState
from app.domain.events import Channel, build_message_in_event, build_message_out_event
from app.domain.hot_handoff import KIND_HOT_LEAD, apply_hot_handoff
from app.domain.owner_notification_delivery import (
    KIND_WEBSITE_HANDOFF_DELIVERY,
    WEBSITE_HANDOFF_DELIVERY_KINDS,
)
from app.domain.sales import FitLevel, PainLevel
from app.domain.website_handoff_brief import KIND_WEBSITE_WHATSAPP
from app.main import app
from app.services import finalization as finalization_module
from app.services import notifications as notifications_mod
from app.services.finalization import (
    KIND,
    ConversationSummary,
    build_conversation_summary,
    finalize_website_conversation,
    kind_for,
    qualify_and_finalize,
)
from app.services.notifications import OwnerTelegramDelivery, render_conversation_summary
from fastapi.testclient import TestClient
from sqlalchemy import event, select
from sqlalchemy.orm import Session, sessionmaker

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
        }
    )
    assert "Name: Dana" in text
    assert "Budget" not in text
    assert "Conversation ID: web_1" in text


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
        recipient_claim = db.get(
            OwnerNotificationRecipientClaimRow, (KIND, lead_id, session_id, "111")
        )
        assert recipient_claim is not None
        assert (
            db.get(
                OwnerNotificationRecipientClaimRow,
                (KIND, lead_id, "some_other_session", "111"),
            )
            is None
        )
    finally:
        db.close()


def test_finalization_commit_failure_sends_nothing_then_retries_once(
    monkeypatch,
) -> None:
    telegram = _patch_owner_send(monkeypatch)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_final_commit_retry"
        lead_id = _seed_turns(
            store,
            session_id=session_id,
            turns=[("prospect", "שלום, רוצה להתקדם")],
        )
        db.commit()
        summary = ConversationSummary(
            conversation_id=session_id,
            lead_id=lead_id,
            recommended_next_step="call",
        )
        original_commit = Session.commit
        injected = False

        def fail_first_commit(db_session):
            nonlocal injected
            if not injected:
                injected = True
                raise RuntimeError("injected finalization claim commit failure")
            return original_commit(db_session)

        monkeypatch.setattr(Session, "commit", fail_first_commit)
        first = finalize_website_conversation(
            store, summary=summary, settings=_owner_settings()
        )
        assert (first.claimed, first.sent, first.duplicate) == (False, False, False)
        assert telegram.sends == []
        assert not store.has_owner_notification_delivery_claim(
            kind=KIND, lead_id=lead_id, notification_key=session_id
        )

        retry = finalize_website_conversation(
            store, summary=summary, settings=_owner_settings()
        )
        duplicate = finalize_website_conversation(
            store, summary=summary, settings=_owner_settings()
        )

        assert (retry.claimed, retry.sent, retry.duplicate) == (True, True, False)
        assert (duplicate.claimed, duplicate.sent, duplicate.duplicate) == (
            False,
            False,
            True,
        )
        assert len(telegram.sends) == 1
    finally:
        db.close()


def test_finalization_release_commit_failure_recovers_then_retries_once(
    monkeypatch,
) -> None:
    sends: list[tuple[str, ...]] = []

    def deliver(*, recipient_ids=None, **kwargs) -> OwnerTelegramDelivery:
        del kwargs
        recipients = tuple(recipient_ids or ())
        sends.append(recipients)
        if len(sends) == 1:
            return OwnerTelegramDelivery(rejected=recipients)
        return OwnerTelegramDelivery(delivered=recipients)

    monkeypatch.setattr(finalization_module, "deliver_owner_telegram", deliver)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        summary = ConversationSummary(
            conversation_id="web_final_release_retry",
            lead_id="lead_final_release_retry",
        )
        original_commit = Session.commit
        commit_calls = 0

        def fail_first_release_commit(db_session):
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 2:
                raise RuntimeError("injected finalization release commit failure")
            return original_commit(db_session)

        monkeypatch.setattr(Session, "commit", fail_first_release_commit)
        rejected = finalize_website_conversation(
            store, summary=summary, settings=_owner_settings()
        )
        retry = finalize_website_conversation(
            store, summary=summary, settings=_owner_settings()
        )
        duplicate = finalize_website_conversation(
            store, summary=summary, settings=_owner_settings()
        )

        assert rejected.claimed and not rejected.sent
        assert retry.sent
        assert duplicate.duplicate
        assert sends == [("111",), ("111",)]
        assert commit_calls >= 4
    finally:
        db.close()


def test_legacy_completed_conversation_claim_is_retained_after_recipient_upgrade(
    monkeypatch,
) -> None:
    """A pre-migration claim is ambiguous delivery, never a recipient backfill."""
    telegram = _patch_owner_send(monkeypatch)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_legacy_done01"
        lead_id = _seed_turns(
            store, session_id=session_id, turns=[("prospect", "רוצה להתקדם")]
        )
        assert store.try_claim_owner_notification(
            kind=KIND,
            lead_id=lead_id,
            conversation_id=session_id,
            claimed_at="2026-08-28T08:00:00+00:00",
        )
        db.commit()

        replayed = qualify_and_finalize(
            store,
            session_id=session_id,
            lead_id=lead_id,
            settings=_owner_settings(),
            next_step="inactivity",
            require_visitor_message=True,
        )

        assert replayed is not None
        assert (replayed.claimed, replayed.sent, replayed.duplicate) == (False, False, True)
        assert telegram.sends == []
        assert db.get(
            OwnerNotificationRecipientClaimRow, (KIND, lead_id, session_id, "111")
        ) is None
        assert db.get(OwnerNotificationClaimRow, (KIND, lead_id, session_id)) is not None
    finally:
        db.close()


def test_kill_switch_suppression_does_not_consume_finalization_claim(monkeypatch) -> None:
    telegram = _patch_owner_send(monkeypatch)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_kill_retry01"
        lead_id = _seed_turns(
            store, session_id=session_id, turns=[("prospect", "שלום, רוצה להתקדם")]
        )
        db.commit()
        blocked = qualify_and_finalize(
            store,
            session_id=session_id,
            lead_id=lead_id,
            settings=_owner_settings(kill_switch=True),
            next_step="session_closed",
            require_visitor_message=True,
        )
        assert blocked is not None
        assert (blocked.claimed, blocked.sent, blocked.duplicate) == (False, False, False)
        assert not store.has_owner_notification_claim(
            kind=KIND, lead_id=lead_id, conversation_id=session_id
        )
        delivered = qualify_and_finalize(
            store,
            session_id=session_id,
            lead_id=lead_id,
            settings=_owner_settings(),
            next_step="session_closed",
            require_visitor_message=True,
        )
        assert delivered is not None
        assert (delivered.claimed, delivered.sent, delivered.duplicate) == (True, True, False)
        assert len(telegram.sends) == 1
    finally:
        db.close()


def test_confirmed_full_rejection_releases_finalization_claim(monkeypatch) -> None:
    class _RejectedTelegram(_RecordingTelegram):
        def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
            self.sends.append(json)
            return httpx.Response(400, json={"ok": False})

    telegram = _RejectedTelegram()
    monkeypatch.setattr(notifications_mod.httpx, "Client", lambda **kwargs: telegram)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        summary = ConversationSummary(
            conversation_id="web_rejected01", lead_id="lead_rejected01"
        )
        result = finalize_website_conversation(
            store, summary=summary, settings=_owner_settings()
        )
        assert result.claimed is True
        assert result.sent is False
        db.commit()
        assert not store.has_owner_notification_claim(
            kind=KIND, lead_id=summary.lead_id, conversation_id=summary.conversation_id
        )
    finally:
        db.close()


def test_finalization_recipient_ledger_retries_only_known_rejection(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_delivery(*, recipient_ids=None, **kwargs) -> OwnerTelegramDelivery:
        del kwargs
        ids = tuple(recipient_ids or ())
        calls.append(ids)
        if ids == ("111", "222"):
            return OwnerTelegramDelivery(delivered=("111",), rejected=("222",))
        return OwnerTelegramDelivery(delivered=ids)

    monkeypatch.setattr(finalization_module, "deliver_owner_telegram", fake_delivery)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        summary = ConversationSummary(conversation_id="web_partial", lead_id="lead_partial")
        settings = Settings(telegram_bot_token="tok", telegram_owner_user_ids="111,222")
        assert finalize_website_conversation(store, summary=summary, settings=settings).sent
        assert finalize_website_conversation(store, summary=summary, settings=settings).sent
        assert calls == [("111", "222"), ("222",)]
    finally:
        db.close()


def test_finalization_no_config_replays_when_delivery_becomes_available(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_delivery(*, recipient_ids=None, **kwargs) -> OwnerTelegramDelivery:
        del kwargs
        ids = tuple(recipient_ids or ())
        calls.append(ids)
        return OwnerTelegramDelivery(delivered=ids)

    monkeypatch.setattr(finalization_module, "deliver_owner_telegram", fake_delivery)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        summary = ConversationSummary(conversation_id="web_later", lead_id="lead_later")
        first = finalize_website_conversation(store, summary=summary, settings=Settings())
        second = finalize_website_conversation(
            store,
            summary=summary,
            settings=Settings(telegram_bot_token="tok", telegram_owner_user_ids="111"),
        )
        assert first.claimed and not first.sent
        assert second.sent
        assert calls == [("111",)]
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


def test_concurrent_hot_and_click_claims_share_one_canonical_winner(tmp_path) -> None:
    engine = make_engine(f"sqlite:///{tmp_path / 'handoff-race.db'}")
    try:
        Base.metadata.create_all(bind=engine)
        factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)
        loser = factory()
        racer = factory()
        claim = {
            "kind": KIND_WEBSITE_HANDOFF_DELIVERY,
            "compatible_kinds": WEBSITE_HANDOFF_DELIVERY_KINDS,
            "lead_id": "lead_cross_path_race",
            "notification_key": "",
            "recipient_id": "111",
            "claimed_at": "2026-08-31T09:00:00+00:00",
        }
        fired: list[str] = []

        @event.listens_for(engine, "before_cursor_execute")
        def _let_other_path_claim(conn, cursor, statement, parameters, context, many):
            del conn, cursor, parameters, context, many
            if fired or "owner_notification_recipient_claims" not in statement.lower():
                return
            if not statement.lstrip().upper().startswith("INSERT"):
                return
            fired.append(statement)
            assert LeadStore(racer).try_claim_owner_notification_recipient_compatible(
                **claim
            )
            racer.commit()

        try:
            claimed = LeadStore(
                loser
            ).try_claim_owner_notification_recipient_compatible(**claim)
            loser.commit()
        finally:
            event.remove(engine, "before_cursor_execute", _let_other_path_claim)

        assert fired, "the cross-path racing writer never claimed"
        assert claimed is False
        rows = list(loser.scalars(select(OwnerNotificationRecipientClaimRow)))
        assert [(row.kind, row.lead_id, row.recipient_id) for row in rows] == [
            (KIND_WEBSITE_HANDOFF_DELIVERY, claim["lead_id"], "111")
        ]
        loser.close()
        racer.close()
    finally:
        engine.dispose()


def test_apply_hot_handoff_twice_sends_one_brief(monkeypatch) -> None:
    """Defect 3. The persist call returned None, so every retry re-sent the same brief."""
    telegram = _patch_owner_send(monkeypatch)
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


def test_hot_handoff_commit_failure_sends_nothing_then_retries_once(
    monkeypatch,
) -> None:
    telegram = _patch_owner_send(monkeypatch)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_hot_commit_retry"
        )
        db.commit()
        original_commit = Session.commit
        injected = False

        def fail_first_commit(db_session):
            nonlocal injected
            if not injected:
                injected = True
                raise RuntimeError("injected hot-handoff claim commit failure")
            return original_commit(db_session)

        monkeypatch.setattr(Session, "commit", fail_first_commit)
        first = apply_hot_handoff(
            store,
            lead_id=lead_id,
            inbound_id="in_hot_commit_1",
            want="רוצה לדבר עכשיו",
            kill_switch=False,
            settings=_owner_settings(),
        )
        assert (first.attempted, first.known_unreachable, first.delivered) == (
            False,
            True,
            (),
        )
        assert telegram.sends == []
        assert store.get_takeover_state(lead_id) == TakeoverState.MIA_ACTIVE.value
        assert not store.has_owner_notification_delivery_claim(
            kind=KIND_HOT_LEAD, lead_id=lead_id
        )

        retry = apply_hot_handoff(
            store,
            lead_id=lead_id,
            inbound_id="in_hot_commit_2",
            want="רוצה לדבר עכשיו",
            kill_switch=False,
            settings=_owner_settings(),
        )
        duplicate = apply_hot_handoff(
            store,
            lead_id=lead_id,
            inbound_id="in_hot_commit_3",
            want="רוצה לדבר עכשיו",
            kill_switch=False,
            settings=_owner_settings(),
        )

        assert retry.delivered == ("111",)
        assert duplicate.attempted is False
        assert len(telegram.sends) == 1
    finally:
        db.close()


def test_hot_handoff_release_commit_failure_recovers_then_retries_once(
    monkeypatch,
) -> None:
    sends: list[tuple[str, ...]] = []

    def deliver(**kwargs) -> OwnerTelegramDelivery:
        recipients = kwargs["recipient_ids"]
        sends.append(recipients)
        if len(sends) == 1:
            return OwnerTelegramDelivery(rejected=recipients)
        return OwnerTelegramDelivery(delivered=recipients)

    monkeypatch.setattr(hot_handoff_module, "_deliver_owners", deliver)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_hot_release_retry"
        )
        db.commit()
        original_commit = Session.commit
        commit_calls = 0

        def fail_first_release_commit(db_session):
            nonlocal commit_calls
            commit_calls += 1
            if commit_calls == 2:
                raise RuntimeError("injected hot-handoff release commit failure")
            return original_commit(db_session)

        monkeypatch.setattr(Session, "commit", fail_first_release_commit)
        rejected = apply_hot_handoff(
            store,
            lead_id=lead_id,
            inbound_id="in_hot_release_1",
            want="human",
            kill_switch=False,
            settings=_owner_settings(),
        )
        retry = apply_hot_handoff(
            store,
            lead_id=lead_id,
            inbound_id="in_hot_release_2",
            want="human",
            kill_switch=False,
            settings=_owner_settings(),
        )
        duplicate = apply_hot_handoff(
            store,
            lead_id=lead_id,
            inbound_id="in_hot_release_3",
            want="human",
            kill_switch=False,
            settings=_owner_settings(),
        )

        assert rejected.attempted and not rejected.delivered
        assert retry.delivered == ("111",)
        assert duplicate.attempted is False
        assert sends == [("111",), ("111",)]
        assert commit_calls >= 4
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
        for label in (
            "Name",
            "Contact",
            "What they need",
            "Main problem",
            "Service they appear interested in",
            "Timeline",
            "Budget",
            "Qualification",
            "Meeting",
        ):
            assert label in labels, text
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
        assert "Budget" not in text
        assert "Timeline" not in text
        assert "Contact" not in text
        assert "Qualification" not in text
    finally:
        db.close()


def test_website_session_end_route_pings_the_owner_exactly_once(monkeypatch) -> None:
    from app.api.deps import get_telegram_port
    from app.integrations.base import RecordingMessagePort

    from tests.conftest import identify_website_visitor

    port = RecordingMessagePort()
    app.dependency_overrides[get_telegram_port] = lambda: port
    settings = _owner_settings()
    monkeypatch.setattr("app.api.website.get_settings", lambda: settings)
    init_db()
    try:
        with TestClient(app) as client:
            session_id = client.post("/v1/website/sessions").json()["session_id"]
            identify_website_visitor(
                client,
                session_id,
                name="יוסי",
                text="היי, קוראים לי יוסי ואני מוכר שעונים",
            )
            ended = client.post(f"/v1/website/sessions/{session_id}/end")
            again = client.post(f"/v1/website/sessions/{session_id}/end")
        assert ended.status_code == 200
        assert ended.json()["finalized"] is True
        assert again.json()["finalized"] is False
        assert len(port.sent) == 1
        sent = port.sent[0].text
        assert "שיחה מהאתר" in sent
        assert "יוסי" in sent
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)


def test_website_session_end_route_is_idempotent(monkeypatch) -> None:
    from app.api.deps import get_telegram_port
    from app.integrations.base import RecordingMessagePort

    from tests.conftest import identify_website_visitor

    port = RecordingMessagePort()
    app.dependency_overrides[get_telegram_port] = lambda: port
    settings = _owner_settings()
    monkeypatch.setattr("app.api.website.get_settings", lambda: settings)
    init_db()
    try:
        with TestClient(app) as client:
            session_id = client.post("/v1/website/sessions").json()["session_id"]
            identify_website_visitor(client, session_id)
            first = client.post(f"/v1/website/sessions/{session_id}/end")
            second = client.post(f"/v1/website/sessions/{session_id}/end")
        assert first.json()["finalized"] is True
        assert second.json()["finalized"] is False
        assert len(port.sent) == 1
    finally:
        app.dependency_overrides.pop(get_telegram_port, None)


def test_website_session_end_without_visitor_turn_is_not_finalized() -> None:
    init_db()
    with TestClient(app) as client:
        session_id = client.post("/v1/website/sessions").json()["session_id"]
        ended = client.post(f"/v1/website/sessions/{session_id}/end")
        assert ended.status_code == 200
        assert ended.json() == {"accepted": True, "finalized": False}


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
        assert finalize_inactive_website_conversations(
            store, settings=settings, principal=Principal.client(source="test"), now=now
        ) == 1
        db.commit()
        assert finalize_inactive_website_conversations(
            store, settings=settings, principal=Principal.client(source="test"), now=now
        ) == 0
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


def test_inactivity_skip_uses_retained_click_claim_not_owner_inbox() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        old = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        failed_session = "web_click_failed_inactive"
        retained_session = "web_click_retained_inactive"
        failed_lead = _seed_turns(
            store,
            session_id=failed_session,
            turns=[("prospect", "שלום")],
            start=old,
        )
        retained_lead = _seed_turns(
            store,
            session_id=retained_session,
            turns=[("prospect", "hello")],
            start=old + timedelta(seconds=1),
        )
        for lead_id in (failed_lead, retained_lead):
            store.upsert_owner_notification(
                kind=KIND_WEBSITE_WHATSAPP,
                lead_id=lead_id,
                scheduled_at=old.isoformat(),
            )
        assert store.try_claim_owner_notification_recipient(
            kind=KIND_WEBSITE_WHATSAPP,
            lead_id=retained_lead,
            notification_key="",
            recipient_id="111",
            claimed_at=old.isoformat(),
        )
        db.commit()

        rows = store.list_inactive_website_conversations(
            cutoff_iso=(old + timedelta(minutes=31)).isoformat(),
            skip_kinds=(KIND_WEBSITE_WHATSAPP,),
            limit=50,
        )
        sessions = {session_id for session_id, _ in rows}
        assert failed_session in sessions
        assert retained_session not in sessions
    finally:
        db.close()


def test_failed_whatsapp_click_inbox_does_not_suppress_later_finalization(
    monkeypatch,
) -> None:
    telegram = _patch_owner_send(monkeypatch)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_click_failed_then_close"
        lead_id = _seed_turns(
            store,
            session_id=session_id,
            turns=[("prospect", "אני רוצה לדבר עם אסף")],
        )
        store.upsert_owner_notification(
            kind=KIND_WEBSITE_WHATSAPP,
            lead_id=lead_id,
            scheduled_at=_START.isoformat(),
        )
        db.commit()

        result = qualify_and_finalize(
            store,
            session_id=session_id,
            lead_id=lead_id,
            settings=_owner_settings(),
            next_step="session_closed",
            require_visitor_message=True,
        )

        assert result is not None and result.sent
        assert len(telegram.sends) == 1
    finally:
        db.close()


def test_retained_whatsapp_click_recipient_claim_suppresses_duplicate_finalization(
    monkeypatch,
) -> None:
    telegram = _patch_owner_send(monkeypatch)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_click_retained_then_close"
        lead_id = _seed_turns(
            store,
            session_id=session_id,
            turns=[("prospect", "אני רוצה לדבר עם אסף")],
        )
        store.upsert_owner_notification(
            kind=KIND_WEBSITE_WHATSAPP,
            lead_id=lead_id,
            scheduled_at=_START.isoformat(),
        )
        assert store.try_claim_owner_notification_recipient(
            kind=KIND_WEBSITE_WHATSAPP,
            lead_id=lead_id,
            notification_key="",
            recipient_id="111",
            claimed_at=_START.isoformat(),
        )
        db.commit()

        result = qualify_and_finalize(
            store,
            session_id=session_id,
            lead_id=lead_id,
            settings=_owner_settings(),
            next_step="session_closed",
            require_visitor_message=True,
        )

        assert result is None
        assert telegram.sends == []
    finally:
        db.close()


def test_empty_returning_session_cannot_borrow_an_old_sessions_visitor_message(
    monkeypatch,
) -> None:
    telegram = _patch_owner_send(monkeypatch)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        old_session = "web_old_message01"
        empty_session = "web_new_empty02"
        customer_id, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=old_session
        )
        db.add(
            ChannelIdentityRow(
                customer_id=customer_id,
                channel=Channel.WEBSITE.value,
                external_id=empty_session,
                verified=False,
            )
        )
        _seed_turns(
            store,
            session_id=old_session,
            lead_id=lead_id,
            turns=[("prospect", "יש לי חנות ואני צריך עזרה")],
        )
        db.commit()

        result = qualify_and_finalize(
            store,
            session_id=empty_session,
            lead_id=lead_id,
            settings=_owner_settings(),
            next_step="inactivity",
            require_visitor_message=True,
        )

        assert result is None
        assert not store.has_owner_notification(kind=KIND, lead_id=lead_id)
        assert db.get(
            OwnerNotificationRecipientClaimRow, (KIND, lead_id, empty_session, "111")
        ) is None
        assert telegram.sends == []
    finally:
        db.close()


def test_inactivity_ignores_empty_returning_session_but_keeps_its_real_conversation() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        old_session = "web_inactive_old01"
        empty_session = "web_inactive_empty02"
        customer_id, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=old_session
        )
        db.add(
            ChannelIdentityRow(
                customer_id=customer_id,
                channel=Channel.WEBSITE.value,
                external_id=empty_session,
                verified=False,
            )
        )
        old = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)
        _seed_turns(
            store,
            session_id=old_session,
            lead_id=lead_id,
            turns=[("prospect", "שלום")],
            start=old,
        )
        db.commit()

        rows = store.list_inactive_website_conversations(
            cutoff_iso=(old + timedelta(minutes=31)).isoformat(), limit=50
        )

        assert (old_session, lead_id) in rows
        assert (empty_session, lead_id) not in rows
    finally:
        db.close()
