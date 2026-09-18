"""TG-DL: the owner execution deadline starts AFTER input preprocessing.

RC1 (app/workers/telegram_owner.py): `deadline_at` used to be computed before
media download, STT, transcript persistence, and the coalesce wait -- so a
slow-but-successful voice transcription (or even just the ordinary coalesce
wait) silently ate into the model's reasoning budget before the model was
ever called. `tests/conftest.py` zeroes `COALESCE_WAIT_S` for the whole
suite (so bursts coalesce instantly in every other test), which is exactly
why no existing test could catch this -- these tests restore a non-zero
value locally to exercise the real preprocessing span.
"""

from __future__ import annotations

import asyncio
from time import monotonic

import pytest
from app.api.owner import OwnerTurnResult
from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.graph.owner_agent import run_owner_agent
from app.integrations.base import RecordingMessagePort
from app.integrations.transcribe import FakeTranscriptionPort
from app.tools.registries.owner_tools import ToolContext
from app.workers import telegram_owner

from tests.unit.test_brain_agent import _assistant_text, _client

ACTOR = "660033"


def _claim(event_id: str, *, envelope_kind: str = "text") -> None:
    init_db()
    db = get_session_factory()()
    try:
        assert LeadStore(db).claim_webhook(
            provider="telegram",
            provider_event_id=event_id,
            channel="telegram",
            envelope_kind=envelope_kind,
        )
        db.commit()
    finally:
        db.close()


def _ctx(settings: Settings) -> ToolContext:
    init_db()
    db = get_session_factory()()
    return ToolContext(
        principal=Principal.owner(source="telegram", actor_id=ACTOR),
        store=LeadStore(db),
        brain=BrainStore(db),
        settings=settings,
        embedding_port=FakeEmbeddingPort(),
        source_ref="telegram:voice-budget",
    )


# ---------------------------------------------------------------------------
# Scenario C -- a slow but SUCCESSFUL STT, followed by a normal owner turn:
# the owner reasoning budget must still be fully available afterwards.
# THIS TEST MUST FAIL ON CURRENT (pre-fix) CODE.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scenario_c_slow_successful_stt_leaves_full_owner_budget(monkeypatch) -> None:
    event_id = "voice-budget-scenario-c"
    _claim(event_id, envelope_kind="audio")
    owner_turn_timeout_seconds = 2.0
    stt_sleep_s = 0.2
    coalesce_wait_s = 0.05
    settings = Settings(
        _env_file=None,
        owner_turn_timeout_seconds=owner_turn_timeout_seconds,
        telegram_owner_user_ids=ACTOR,
    )
    monkeypatch.setattr(telegram_owner, "get_settings", lambda: settings)
    # Restore a real, non-zero coalesce wait for this test only -- the autouse
    # fixture in tests/conftest.py zeroes it for the whole suite.
    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", coalesce_wait_s)

    async def slow_but_successful_stt(*, item, media, transcribe_port):
        del media, transcribe_port
        await asyncio.sleep(stt_sleep_s)
        item["text"] = "תקבעי עם דניאל מחר בשתיים"
        item["source"] = "audio"
        item["stt_provider"] = "fake"
        item["stt_model"] = "fake-model"
        item["language"] = "he"
        item["duration_ms"] = "500"
        item["confidence"] = "0.95"
        item["stt_latency_ms"] = str(int(stt_sleep_s * 1000))
        return item, "", int(stt_sleep_s * 1000)

    monkeypatch.setattr(
        "app.api.telegram._transcribe_telegram_voice", slow_but_successful_stt
    )

    captured: dict[str, float] = {}

    async def fake_owner_loop(
        *, item, store, port, settings, owner_ids, deadline_at=None, delivery_state=None
    ):
        del store, owner_ids
        captured["remaining"] = (
            deadline_at - monotonic() if deadline_at is not None else float("inf")
        )
        await port.send(
            telegram_owner.outbound_reply(item, text="real answer", channel=Channel.TELEGRAM)
        )
        if delivery_state is not None:
            delivery_state["sent"] = True
        return OwnerTurnResult(processed=True, sent=True, last_reply="real answer")

    monkeypatch.setattr(telegram_owner, "run_owner_loop", fake_owner_loop)
    port = RecordingMessagePort()

    await telegram_owner.process_telegram_owner_update(
        item={"id": event_id, "from": ACTOR, "chat_id": ACTOR, "message_id": "1", "text": ""},
        envelope_kind="audio",
        voice_file_id="voice-file-1",
        port=port,
        transcribe_port=FakeTranscriptionPort("unused"),
    )

    # The real answer reached Telegram -- not a timeout notice.
    assert len(port.sent) == 1
    assert port.sent[0].text == "real answer"
    # The load-bearing assertion: by the time the owner loop actually started,
    # almost the FULL owner_turn_timeout_seconds must still be on the clock --
    # the STT sleep and the coalesce wait must not have been deducted from it.
    # On the pre-fix code, deadline_at was computed BEFORE either of those, so
    # `remaining` here would be roughly `owner_turn_timeout_seconds - (stt_sleep_s
    # + coalesce_wait_s)` -- well under this threshold.
    assert captured["remaining"] >= owner_turn_timeout_seconds - 0.15, captured["remaining"]


def test_text_only_coalescing_does_not_consume_reasoning_time(monkeypatch) -> None:
    """Same proof as Scenario C, isolated to the coalesce wait alone (no voice)."""
    asyncio.run(_text_only_coalescing_does_not_consume_reasoning_time(monkeypatch))


async def _text_only_coalescing_does_not_consume_reasoning_time(monkeypatch) -> None:
    event_id = "voice-budget-text-coalesce"
    _claim(event_id, envelope_kind="text")
    owner_turn_timeout_seconds = 1.0
    coalesce_wait_s = 0.15
    settings = Settings(
        _env_file=None,
        owner_turn_timeout_seconds=owner_turn_timeout_seconds,
        telegram_owner_user_ids=ACTOR,
    )
    monkeypatch.setattr(telegram_owner, "get_settings", lambda: settings)
    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", coalesce_wait_s)

    captured: dict[str, float] = {}

    async def fake_owner_loop(
        *, item, store, port, settings, owner_ids, deadline_at=None, delivery_state=None
    ):
        del store, owner_ids
        captured["remaining"] = (
            deadline_at - monotonic() if deadline_at is not None else float("inf")
        )
        await port.send(
            telegram_owner.outbound_reply(item, text="real answer", channel=Channel.TELEGRAM)
        )
        if delivery_state is not None:
            delivery_state["sent"] = True
        return OwnerTurnResult(processed=True, sent=True, last_reply="real answer")

    monkeypatch.setattr(telegram_owner, "run_owner_loop", fake_owner_loop)
    port = RecordingMessagePort()

    await telegram_owner.process_telegram_owner_update(
        item={"id": event_id, "from": ACTOR, "chat_id": ACTOR, "text": "מה המצב"},
        envelope_kind="text",
        voice_file_id=None,
        port=port,
        transcribe_port=FakeTranscriptionPort("unused"),
    )

    assert len(port.sent) == 1
    assert port.sent[0].text == "real answer"
    assert captured["remaining"] >= owner_turn_timeout_seconds - 0.08, captured["remaining"]


# ---------------------------------------------------------------------------
# Scenario B -- the plain text "?" exercises the normal model-led path and
# completes with one simple model response and no tools.
# ---------------------------------------------------------------------------


def test_scenario_b_bare_question_mark_completes_with_one_model_response() -> None:
    session_client, transport = _client([_assistant_text("פה. מה צריך?")])
    settings = Settings(_env_file=None)
    ctx = _ctx(settings)

    outcome = run_owner_agent(
        client=session_client,
        ctx=ctx,
        owner_message="?",
        deadline_at=monotonic() + 30.0,
    )

    assert outcome.completed is True
    assert outcome.tools_used == ()
    assert len(transport.requests) == 1


# ---------------------------------------------------------------------------
# STT failure must still produce the STT-specific reply, never the
# owner-timeout reply -- and must never reach the owner loop at all.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stt_failure_sends_stt_specific_reply_not_owner_timeout(monkeypatch) -> None:
    event_id = "voice-budget-stt-failure"
    _claim(event_id, envelope_kind="audio")
    settings = Settings(
        _env_file=None, owner_turn_timeout_seconds=45.0, telegram_owner_user_ids=ACTOR
    )
    monkeypatch.setattr(telegram_owner, "get_settings", lambda: settings)
    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", 0)

    async def failing_stt(*, item, media, transcribe_port):
        del media, transcribe_port
        return item, "stt_failed", 5

    monkeypatch.setattr("app.api.telegram._transcribe_telegram_voice", failing_stt)

    stt_reply_marker = "STT_SPECIFIC_REPLY_MARKER"

    async def fake_failure_reply(*, item, port, kill_switch, automation_mode):
        del kill_switch, automation_mode
        await port.send(
            telegram_owner.outbound_reply(item, text=stt_reply_marker, channel=Channel.TELEGRAM)
        )
        return True

    monkeypatch.setattr(
        "app.api.telegram._send_transcription_failure_reply", fake_failure_reply
    )

    def forbidden(**_kwargs):
        raise AssertionError("an STT failure must never reach the owner loop")

    monkeypatch.setattr(telegram_owner, "run_owner_loop", forbidden)
    port = RecordingMessagePort()

    await telegram_owner.process_telegram_owner_update(
        item={"id": event_id, "from": ACTOR, "chat_id": ACTOR, "message_id": "1", "text": ""},
        envelope_kind="audio",
        voice_file_id="voice-file-2",
        port=port,
        transcribe_port=FakeTranscriptionPort("unused"),
    )

    assert len(port.sent) == 1
    assert port.sent[0].text == stt_reply_marker
    assert "תם הזמן" not in port.sent[0].text
