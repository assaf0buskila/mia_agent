"""Owner burst stitch, last-toolkit continue, and Telegram hang fallback."""

from __future__ import annotations

import asyncio
import json
import threading

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
from sqlalchemy import text as sql_text


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
    monkeypatch.setenv("MIA_OWNER_TURN_TIMEOUT_SECONDS", "0.05")

    drained_with_open_session: list[bool] = []

    async def hang(**kwargs):
        await asyncio.sleep(0.1)
        # The hard deadline has elapsed, but the worker must drain this work before
        # closing the DB session it owns.
        assert kwargs["store"].session.execute(sql_text("SELECT 1")).scalar_one() == 1
        drained_with_open_session.append(True)

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
    assert drained_with_open_session == [True]
    assert "תם הזמן" in port.sent[0].text
    assert HANG_REPLY.split("(")[0] in port.sent[0].text or "תם הזמן" in port.sent[0].text


@pytest.mark.asyncio
async def test_typing_action_renews_until_stop() -> None:
    from app.workers.telegram_owner import _renew_typing

    class TypingPort(RecordingMessagePort):
        def __init__(self) -> None:
            super().__init__()
            self.actions: list[tuple[str, str]] = []

        async def send_chat_action(self, chat_id: str, *, action: str = "typing") -> None:
            self.actions.append((chat_id, action))

    port = TypingPort()
    stop = asyncio.Event()
    task = asyncio.create_task(_renew_typing(port=port, chat_id="99", interval_s=0.01, stop=stop))
    await asyncio.sleep(0.035)
    stop.set()
    await task
    assert len(port.actions) >= 3
    assert set(port.actions) == {("99", "typing")}


@pytest.mark.asyncio
async def test_worker_cancellation_drains_owner_turn_before_session_close(monkeypatch) -> None:
    from app.workers import telegram_owner

    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", 0)
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", "99")
    drained: list[bool] = []

    async def slow(**kwargs):
        await asyncio.sleep(0.05)
        assert kwargs["store"].session.execute(sql_text("SELECT 1")).scalar_one() == 1
        drained.append(True)

    monkeypatch.setattr(telegram_owner, "run_owner_loop", slow)
    reset_pending_turns()
    init_db()
    db = get_session_factory()()
    try:
        assert LeadStore(db).claim_webhook(
            provider="telegram",
            provider_event_id="tg-cancel-drain-1",
            channel="telegram",
        )
        db.commit()
    finally:
        db.close()

    task = asyncio.create_task(
        process_telegram_owner_update(
            item={
                "id": "tg-cancel-drain-1",
                "from": "99",
                "chat_id": "99",
                "text": "check",
            },
            envelope_kind="text",
            voice_file_id=None,
            port=RecordingMessagePort(),
            transcribe_port=FakeTranscriptionPort("x"),
        )
    )
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert drained == [True]


@pytest.mark.asyncio
async def test_cancellation_during_deadline_drain_keeps_session_open_for_sync_work(
    monkeypatch,
) -> None:
    from app.surfaces import owner as owner_surface
    from app.workers import telegram_owner

    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", 0)
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", "99")
    monkeypatch.setenv("MIA_OWNER_TURN_TIMEOUT_SECONDS", "0.02")
    entered = threading.Event()
    release = threading.Event()
    drained: list[bool] = []

    def blocking_talk(**kwargs):
        entered.set()
        assert release.wait(timeout=2)
        assert kwargs["store"].session.execute(sql_text("SELECT 1")).scalar_one() == 1
        drained.append(True)
        return "late", False

    monkeypatch.setattr(owner_surface, "_talk_with_optional_agent", blocking_talk)
    reset_pending_turns()
    init_db()
    db = get_session_factory()()
    try:
        assert LeadStore(db).claim_webhook(
            provider="telegram",
            provider_event_id="tg-cancel-deadline-drain-1",
            channel="telegram",
        )
        db.commit()
    finally:
        db.close()

    task = asyncio.create_task(
        process_telegram_owner_update(
            item={
                "id": "tg-cancel-deadline-drain-1",
                "from": "99",
                "chat_id": "99",
                "text": "check the current pipeline",
            },
            envelope_kind="text",
            voice_file_id=None,
            port=RecordingMessagePort(),
            transcribe_port=FakeTranscriptionPort("x"),
        )
    )
    assert await asyncio.to_thread(entered.wait, 1)
    await asyncio.sleep(0.04)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert drained == [True]


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


@pytest.mark.asyncio
async def test_live_telegram_voice_persists_transcript_and_provenance(monkeypatch) -> None:
    from app.surfaces import turn_coalesce
    from app.workers import telegram_owner

    class VoicePort(RecordingMessagePort):
        async def download_voice(self, *_args, **_kwargs):
            return b"voice-bytes", "audio/ogg", "note.ogg"

    monkeypatch.setattr(turn_coalesce, "COALESCE_WAIT_S", 0)
    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", 0)
    monkeypatch.setenv("MIA_TELEGRAM_OWNER_USER_IDS", "99")
    monkeypatch.setenv("MIA_OWNER_AGENT_MODEL", "")
    reset_pending_turns()
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        assert store.claim_webhook(
            provider="telegram",
            provider_event_id="tg-voice-live-1",
            channel="telegram",
            envelope_kind="audio",
        )
        db.commit()
    finally:
        db.close()

    transcript = "תקבעי עם דניאל מחר בשתיים"
    await process_telegram_owner_update(
        item={
            "id": "tg-voice-live-1",
            "from": "99",
            "chat_id": "99",
            "message_id": "5",
            "text": "",
            "mime_type": "audio/ogg",
        },
        envelope_kind="audio",
        voice_file_id="voice-file",
        port=VoicePort(),
        transcribe_port=FakeTranscriptionPort(transcript),
    )

    verify = get_session_factory()()
    try:
        stored = LeadStore(verify)
        row = stored.get_transcript(provider="telegram", provider_event_id="tg-voice-live-1")
        assert row is not None
        assert row.transcript == transcript
        assert row.stt_provider == "fake"
        outcome = stored.get_canonical_event(
            provider="telegram",
            provider_event_id="tg-voice-live-1:tool:voice_transcribe",
        )
        assert outcome is not None
        assert json.loads(outcome.payload_json)["status"] == "ok"
    finally:
        verify.close()
