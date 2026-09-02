"""Owner burst stitch, last-toolkit continue, and Telegram hang fallback."""

from __future__ import annotations

import asyncio

import pytest
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.memory import ConversationTurn
from app.integrations.base import RecordingMessagePort
from app.integrations.transcribe import FakeTranscriptionPort
from app.surfaces.turn_coalesce import (
    HANG_REPLY,
    claim_burst,
    detect_asked_toolkit,
    enqueue_turn,
    merge_claimed_items,
    prepare_owner_utterance,
    reset_pending_turns,
    stitch_texts,
    take_if_still_pending,
)
from app.workers.telegram_owner import process_telegram_owner_update


def test_stitch_and_claim_keeps_newest_as_leader() -> None:
    reset_pending_turns()
    enqueue_turn("chat-1", {"id": "1", "text": "what do you see on the site"})
    enqueue_turn("chat-1", {"id": "2", "text": "improvements"})
    enqueue_turn("chat-1", {"id": "3", "text": "?"})
    assert claim_burst("chat-1", "2") is None
    claimed = claim_burst("chat-1", "3")
    assert claimed is not None
    assert merge_claimed_items(claimed)["text"] == stitch_texts(
        ["what do you see on the site", "improvements", "?"]
    )
    assert take_if_still_pending("chat-1", "3") is None


def test_continue_follows_last_asked_ga_gsc_not_instagram() -> None:
    history = (
        ConversationTurn(role="owner", text="instagram views"),
        ConversationTurn(role="mia", text="named posts"),
        ConversationTurn(role="owner", text="GA and Search Console please"),
        ConversationTurn(role="mia", text="GA4 property unknown"),
    )
    assert detect_asked_toolkit("תמשיך נתונים") == ""
    prepared = prepare_owner_utterance("תמשיך נתונים", history)
    assert "last asked toolkit (GA/GSC)" in prepared
    assert "instagram views" not in prepared.lower()


def test_asked_toolkit_first_prefix() -> None:
    prepared = prepare_owner_utterance("show me GSC clicks", ())
    assert prepared.startswith("Answer the asked toolkit first (GA/GSC)")


@pytest.mark.asyncio
async def test_telegram_timeout_sends_fallback_not_silence(monkeypatch) -> None:
    from app.surfaces import turn_coalesce
    from app.workers import telegram_owner

    monkeypatch.setattr(turn_coalesce, "COALESCE_WAIT_S", 0)
    monkeypatch.setattr(turn_coalesce, "OWNER_TURN_TIMEOUT_S", 0.05)
    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", 0)
    monkeypatch.setattr(telegram_owner, "OWNER_TURN_TIMEOUT_S", 0.05)

    async def hang(**kwargs):
        del kwargs
        await asyncio.sleep(2)

    monkeypatch.setattr(telegram_owner, "run_owner_loop", hang)
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        item = {
            "id": "tg-hang-1",
            "from": "99",
            "chat_id": "99",
            "text": "GA",
            "message_id": "1",
        }
        assert store.claim_webhook(
            provider="telegram",
            provider_event_id="tg-hang-1",
            channel="telegram",
        )
        db.commit()
    finally:
        db.close()

    port = RecordingMessagePort()
    await process_telegram_owner_update(
        item=item,
        envelope_kind="text",
        voice_file_id=None,
        port=port,
        transcribe_port=FakeTranscriptionPort("x"),
    )
    assert port.sent
    assert "תם הזמן" in port.sent[0].text
    assert HANG_REPLY.split("(")[0] in port.sent[0].text or "תם הזמן" in port.sent[0].text


@pytest.mark.asyncio
async def test_telegram_burst_is_one_owner_turn(monkeypatch) -> None:
    from app.surfaces import turn_coalesce
    from app.workers import telegram_owner

    monkeypatch.setattr(turn_coalesce, "COALESCE_WAIT_S", 0.05)
    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", 0.05)
    seen: list[str] = []

    async def capture(*, item, **kwargs):
        del kwargs
        seen.append(item.get("text") or "")

    monkeypatch.setattr(telegram_owner, "run_owner_loop", capture)
    init_db()
    db = get_session_factory()()
    items = [
        {"id": "tg-a", "from": "99", "chat_id": "99", "text": "what do you see on the site"},
        {"id": "tg-b", "from": "99", "chat_id": "99", "text": "improvements"},
        {"id": "tg-c", "from": "99", "chat_id": "99", "text": "?"},
        {"id": "tg-d", "from": "99", "chat_id": "99", "text": "Mia"},
    ]
    try:
        store = LeadStore(db)
        for item in items:
            store.claim_webhook(
                provider="telegram",
                provider_event_id=item["id"],
                channel="telegram",
            )
        db.commit()
    finally:
        db.close()

    port = RecordingMessagePort()
    transcribe = FakeTranscriptionPort("x")
    await asyncio.gather(
        *[
            process_telegram_owner_update(
                item=item,
                envelope_kind="text",
                voice_file_id=None,
                port=port,
                transcribe_port=transcribe,
            )
            for item in items
        ]
    )
    assert len(seen) == 1
    assert "what do you see on the site" in seen[0]
    assert "improvements" in seen[0]
    assert "Mia" in seen[0]
