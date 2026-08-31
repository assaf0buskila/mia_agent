"""Website HANDOFF must notify the owner or fail closed without a lying transfer claim.

Live: Ask Mia told a visitor the conversation was transferred to Assaf. Assaf got
nothing on Telegram, and the visitor was not put in contact with him. Visitor WhatsApp
send can stay gated (`MIA_WHATSAPP_HANDOFF_SEND=false`); the owner ping must still fire,
and the widget copy must not claim a transfer Telegram never accepted.
"""

from __future__ import annotations

import httpx
from app.agents.client.graph import compile_client_graph
from app.capabilities.types import Principal
from app.channels.website import message_to_client_state
from app.core.config import Settings
from app.db.models import OwnerNotificationRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.hot_handoff import KIND_HOT_LEAD, apply_hot_handoff
from app.domain.website_handoff_brief import (
    KIND_WEBSITE_WHATSAPP,
    NOTIFICATION_DELIVERED,
    NOTIFICATION_DUPLICATE_OR_AMBIGUOUS,
    apply_website_whatsapp_handoff_brief,
)
from app.graph.replies import (
    HANDOFF_LIE_MARKERS,
    HANDOFF_OWNER_NOTIFIED,
    HANDOFF_OWNER_UNREACHABLE,
)
from app.integrations.sales_reply import CannedSalesReplyPort
from app.services import notifications as notifications_mod
from sqlalchemy import select


class _RecordingTelegram:
    def __init__(self, *, status_code: int = 200, ok: bool = True) -> None:
        self.status_code = status_code
        self.ok = ok
        self.sends: list[dict[str, object]] = []

    def __enter__(self) -> _RecordingTelegram:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
        self.sends.append(json)
        return httpx.Response(self.status_code, json={"ok": self.ok})


class _PerRecipientTelegram(_RecordingTelegram):
    def __init__(self, outcomes: dict[str, tuple[int, bool] | Exception]) -> None:
        super().__init__()
        self.outcomes = outcomes

    def post(self, url: str, *, json: dict[str, object]) -> httpx.Response:
        self.sends.append(json)
        outcome = self.outcomes[str(json["chat_id"])]
        if isinstance(outcome, Exception):
            raise outcome
        status_code, ok = outcome
        return httpx.Response(status_code, json={"ok": ok})


def _patch_telegram(monkeypatch, client: _RecordingTelegram) -> None:
    monkeypatch.setattr(notifications_mod.httpx, "Client", lambda **kwargs: client)


def _drive_handoff(store: LeadStore, session_id: str, lead_id: str, settings: Settings):
    graph = compile_client_graph(
        store,
        reply_port=CannedSalesReplyPort(),
        settings=settings,
        principal=Principal.client(source="website", actor_id=session_id),
    )
    return graph.invoke(
        message_to_client_state(
            run_id="run_handoff",
            session_id=session_id,
            lead_id=lead_id,
            text="לדבר עם אסף",
            inbound_id=f"{session_id}:in1",
        )
    )


def test_website_handoff_notifies_the_owner_and_says_so(monkeypatch) -> None:
    telegram = _RecordingTelegram()
    _patch_telegram(monkeypatch, telegram)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_handoff_ok01"
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=session_id
        )
        db.commit()
        settings = Settings(
            telegram_bot_token="tok",
            telegram_owner_user_ids="111",
            whatsapp_handoff_send=False,
        )
        out = _drive_handoff(store, session_id, lead_id, settings)
        assert out["next_action"] == "handoff"
        assert telegram.sends, "owner Telegram was never called"
        assert len(telegram.sends) == 1
        assert telegram.sends[0]["chat_id"] == "111"
        brief = str(telegram.sends[0]["text"])
        assert "ליד מהאתר" in brief
        assert "צריך אותך" in brief
        assert out["reply"] == HANDOFF_OWNER_NOTIFIED
        for marker in HANDOFF_LIE_MARKERS:
            if marker in HANDOFF_OWNER_NOTIFIED:
                continue
            assert marker not in out["reply"]
    finally:
        db.close()


def test_graph_handoff_and_whatsapp_click_share_one_owner_delivery_both_orders(
    monkeypatch,
) -> None:
    telegram = _RecordingTelegram()
    _patch_telegram(monkeypatch, telegram)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = Settings(
            telegram_bot_token="tok",
            telegram_owner_user_ids="111",
            whatsapp_handoff_send=False,
        )

        graph_first_session = "web_graph_then_click"
        _, graph_first_lead = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=graph_first_session
        )
        db.commit()
        graph_out = _drive_handoff(
            store, graph_first_session, graph_first_lead, settings
        )
        click_after_graph = apply_website_whatsapp_handoff_brief(
            store,
            lead_id=graph_first_lead,
            session_id=graph_first_session,
            settings=settings,
        )

        click_first_session = "web_click_then_graph"
        _, click_first_lead = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=click_first_session
        )
        db.commit()
        click_before_graph = apply_website_whatsapp_handoff_brief(
            store,
            lead_id=click_first_lead,
            session_id=click_first_session,
            settings=settings,
        )
        _drive_handoff(store, click_first_session, click_first_lead, settings)

        assert graph_out["reply"] == HANDOFF_OWNER_NOTIFIED
        assert (
            click_after_graph.notification_status
            == NOTIFICATION_DUPLICATE_OR_AMBIGUOUS
        )
        assert click_before_graph.notification_status == NOTIFICATION_DELIVERED
        assert [send["chat_id"] for send in telegram.sends] == ["111", "111"]
    finally:
        db.close()


def test_legacy_hot_claim_suppresses_graph_and_whatsapp_click(monkeypatch) -> None:
    telegram = _RecordingTelegram()
    _patch_telegram(monkeypatch, telegram)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_legacy_hot_cross_path"
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=session_id
        )
        assert store.try_claim_owner_notification(
            kind=KIND_HOT_LEAD,
            lead_id=lead_id,
            conversation_id="",
            claimed_at="2026-08-31T00:00:00+00:00",
        )
        db.commit()
        settings = Settings(telegram_bot_token="tok", telegram_owner_user_ids="111")

        click = apply_website_whatsapp_handoff_brief(
            store, lead_id=lead_id, session_id=session_id, settings=settings
        )
        _drive_handoff(store, session_id, lead_id, settings)

        assert click.notification_status == NOTIFICATION_DUPLICATE_OR_AMBIGUOUS
        assert telegram.sends == []
    finally:
        db.close()


def test_historical_recipient_claims_suppress_only_that_owner_across_paths(
    monkeypatch,
) -> None:
    telegram = _RecordingTelegram()
    _patch_telegram(monkeypatch, telegram)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        settings = Settings(
            telegram_bot_token="tok", telegram_owner_user_ids="111,222"
        )

        old_hot_session = "web_old_hot_recipient"
        _, old_hot_lead = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=old_hot_session
        )
        assert store.try_claim_owner_notification_recipient(
            kind=KIND_HOT_LEAD,
            lead_id=old_hot_lead,
            recipient_id="111",
            claimed_at="2026-08-31T00:00:00+00:00",
        )
        db.commit()
        apply_website_whatsapp_handoff_brief(
            store,
            lead_id=old_hot_lead,
            session_id=old_hot_session,
            settings=settings,
        )

        old_click_session = "web_old_click_recipient"
        _, old_click_lead = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=old_click_session
        )
        assert store.try_claim_owner_notification_recipient(
            kind=KIND_WEBSITE_WHATSAPP,
            lead_id=old_click_lead,
            recipient_id="111",
            claimed_at="2026-08-31T00:00:00+00:00",
        )
        db.commit()
        _drive_handoff(store, old_click_session, old_click_lead, settings)

        assert [send["chat_id"] for send in telegram.sends] == ["222", "222"]
    finally:
        db.close()


def test_website_handoff_sends_one_card_per_allowlisted_owner(monkeypatch) -> None:
    telegram = _RecordingTelegram()
    _patch_telegram(monkeypatch, telegram)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_handoff_twoowners"
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=session_id
        )
        db.commit()
        out = _drive_handoff(
            store,
            session_id,
            lead_id,
            Settings(telegram_bot_token="tok", telegram_owner_user_ids="111,222"),
        )
        assert out["reply"] == HANDOFF_OWNER_NOTIFIED
        assert [send["chat_id"] for send in telegram.sends] == ["111", "222"]
    finally:
        db.close()


def test_website_handoff_without_telegram_does_not_claim_a_transfer(monkeypatch) -> None:
    telegram = _RecordingTelegram()
    _patch_telegram(monkeypatch, telegram)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_handoff_fail1"
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=session_id
        )
        db.commit()
        out = _drive_handoff(store, session_id, lead_id, Settings())
        assert out["next_action"] == "handoff"
        assert telegram.sends == []
        assert out["reply"] == HANDOFF_OWNER_UNREACHABLE
        for marker in HANDOFF_LIE_MARKERS:
            assert marker not in out["reply"]
        assert "מעבירים לאסף" not in out["reply"]
        assert "אעביר לו" not in out["reply"]
    finally:
        db.close()


def test_website_handoff_telegram_400_is_not_a_delivery(monkeypatch) -> None:
    telegram = _RecordingTelegram(status_code=400, ok=False)
    _patch_telegram(monkeypatch, telegram)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_handoff_40001"
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=session_id
        )
        db.commit()
        settings = Settings(telegram_bot_token="tok", telegram_owner_user_ids="111")
        out = _drive_handoff(store, session_id, lead_id, settings)
        assert telegram.sends, "the send was never attempted"
        assert out["reply"] == HANDOFF_OWNER_UNREACHABLE
        for marker in HANDOFF_LIE_MARKERS:
            assert marker not in out["reply"]
    finally:
        db.close()


def test_website_handoff_retries_owner_ping_after_a_failed_send(monkeypatch) -> None:
    """A claimed-but-failed Telegram must not lock the lead. The next HANDOFF turn
    has to try again, or Assaf never hears about the visitor."""
    telegram = _RecordingTelegram(status_code=400, ok=False)
    _patch_telegram(monkeypatch, telegram)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_handoff_retry1"
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id=session_id
        )
        db.commit()
        settings = Settings(
            telegram_bot_token="tok",
            telegram_owner_user_ids="111",
            whatsapp_handoff_send=False,
        )
        first = _drive_handoff(store, session_id, lead_id, settings)
        assert first["reply"] == HANDOFF_OWNER_UNREACHABLE
        failed_sends = len(telegram.sends)
        assert failed_sends >= 1
        telegram.status_code = 200
        telegram.ok = True
        second = _drive_handoff(store, session_id, lead_id, settings)
        assert len(telegram.sends) > failed_sends
        assert second["reply"] == HANDOFF_OWNER_NOTIFIED
        for marker in HANDOFF_LIE_MARKERS:
            assert marker not in second["reply"]
    finally:
        db.close()


def test_website_handoff_replays_after_missing_telegram_configuration(monkeypatch) -> None:
    telegram = _RecordingTelegram()
    _patch_telegram(monkeypatch, telegram)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_handoff_later_config"
        _, lead_id = store.open_channel_lead(channel=Channel.WEBSITE, external_id=session_id)
        db.commit()
        first = _drive_handoff(store, session_id, lead_id, Settings())
        assert first["reply"] == HANDOFF_OWNER_UNREACHABLE
        assert telegram.sends == []
        inbox = db.scalars(
            select(OwnerNotificationRow).where(OwnerNotificationRow.lead_id == lead_id)
        ).one()
        assert inbox.kind == "hot_lead"
        out = _drive_handoff(
            store,
            session_id,
            lead_id,
            Settings(telegram_bot_token="tok", telegram_owner_user_ids="111"),
        )
        assert out["reply"] == HANDOFF_OWNER_NOTIFIED
        assert [send["chat_id"] for send in telegram.sends] == ["111"]
    finally:
        db.close()


def test_blank_handoff_brief_does_not_consume_a_recipient_claim(monkeypatch) -> None:
    telegram = _RecordingTelegram()
    _patch_telegram(monkeypatch, telegram)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        _, lead_id = store.open_channel_lead(
            channel=Channel.WEBSITE, external_id="web_handoff_blank_brief"
        )
        db.commit()
        settings = Settings(telegram_bot_token="tok", telegram_owner_user_ids="111")
        blank = apply_hot_handoff(
            store,
            lead_id=lead_id,
            inbound_id="blank:in1",
            want="handoff",
            kill_switch=False,
            settings=settings,
            brief="",
        )
        assert blank.known_unreachable
        assert telegram.sends == []
        retry = apply_hot_handoff(
            store,
            lead_id=lead_id,
            inbound_id="blank:in2",
            want="handoff",
            kill_switch=False,
            settings=settings,
            brief="retry text",
        )
        assert retry.delivered == ("111",)
        assert [send["chat_id"] for send in telegram.sends] == ["111"]
    finally:
        db.close()


def test_website_handoff_partial_rejection_retries_only_the_missing_owner(monkeypatch) -> None:
    telegram = _PerRecipientTelegram({"111": (200, True), "222": (400, False)})
    _patch_telegram(monkeypatch, telegram)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_handoff_partial_retry"
        _, lead_id = store.open_channel_lead(channel=Channel.WEBSITE, external_id=session_id)
        db.commit()
        settings = Settings(telegram_bot_token="tok", telegram_owner_user_ids="111,222")
        first = _drive_handoff(store, session_id, lead_id, settings)
        assert first["reply"] == HANDOFF_OWNER_NOTIFIED
        telegram.outcomes["222"] = (200, True)
        second = _drive_handoff(store, session_id, lead_id, settings)
        assert second["reply"] == HANDOFF_OWNER_NOTIFIED
        assert [send["chat_id"] for send in telegram.sends] == ["111", "222", "222"]
    finally:
        db.close()


def test_website_handoff_ambiguous_recipient_is_not_retried(monkeypatch) -> None:
    telegram = _PerRecipientTelegram({"111": (200, True), "222": httpx.ConnectError("down")})
    _patch_telegram(monkeypatch, telegram)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_handoff_ambiguous"
        _, lead_id = store.open_channel_lead(channel=Channel.WEBSITE, external_id=session_id)
        db.commit()
        settings = Settings(telegram_bot_token="tok", telegram_owner_user_ids="111,222")
        first = _drive_handoff(store, session_id, lead_id, settings)
        assert first["reply"] == HANDOFF_OWNER_NOTIFIED
        _drive_handoff(store, session_id, lead_id, settings)
        assert [send["chat_id"] for send in telegram.sends] == ["111", "222"]
    finally:
        db.close()


def test_website_handoff_successful_replay_does_not_resend(monkeypatch) -> None:
    telegram = _RecordingTelegram()
    _patch_telegram(monkeypatch, telegram)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        session_id = "web_handoff_no_resend"
        _, lead_id = store.open_channel_lead(channel=Channel.WEBSITE, external_id=session_id)
        db.commit()
        settings = Settings(telegram_bot_token="tok", telegram_owner_user_ids="111,222")
        _drive_handoff(store, session_id, lead_id, settings)
        _drive_handoff(store, session_id, lead_id, settings)
        assert [send["chat_id"] for send in telegram.sends] == ["111", "222"]
    finally:
        db.close()
