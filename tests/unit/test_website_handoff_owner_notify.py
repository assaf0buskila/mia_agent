"""Website HANDOFF must notify the owner or fail closed without a lying transfer claim.

Live: Ask Mia told a visitor the conversation was transferred to Assaf. Assaf got
nothing on Telegram, and the visitor was not put in contact with him. Visitor WhatsApp
send can stay gated (`MIA_WHATSAPP_HANDOFF_SEND=false`); the owner ping must still fire,
and the widget copy must not claim a transfer Telegram never accepted.
"""

from __future__ import annotations

import httpx
from app.agents.client.graph import compile_client_graph
from app.channels.website import message_to_client_state
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain import hot_handoff as hot_handoff_mod
from app.domain.events import Channel
from app.graph.replies import (
    HANDOFF_LIE_MARKERS,
    HANDOFF_OWNER_NOTIFIED,
    HANDOFF_OWNER_UNREACHABLE,
)
from app.integrations.sales_reply import CannedSalesReplyPort
from app.services import notifications as notifications_mod


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


def _patch_telegram(monkeypatch, client: _RecordingTelegram) -> None:
    monkeypatch.setattr(hot_handoff_mod.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr(notifications_mod.httpx, "Client", lambda **kwargs: client)


def _drive_handoff(store: LeadStore, session_id: str, lead_id: str, settings: Settings):
    graph = compile_client_graph(
        store, reply_port=CannedSalesReplyPort(), settings=settings
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
        assert telegram.sends[0]["chat_id"] == "111"
        brief = str(telegram.sends[0]["text"])
        assert "ליד מהאתר" in brief
        assert "צריך אותך" in brief
        assert "ליד" in brief
        assert "וואטסאפ הוצע" in brief
        assert "\n" in brief
        assert telegram.sends[0].get("parse_mode") == "HTML"
        assert out["reply"] == HANDOFF_OWNER_NOTIFIED
        for marker in HANDOFF_LIE_MARKERS:
            if marker in HANDOFF_OWNER_NOTIFIED:
                continue
            assert marker not in out["reply"]
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
