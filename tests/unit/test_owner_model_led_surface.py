"""Focused lifecycle regressions for the active Telegram owner surface."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from time import monotonic
from types import SimpleNamespace

import pytest
from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.integrations.base import RecordingMessagePort
from app.integrations.sheets import FakeSheetsPort
from app.integrations.transcribe import FakeTranscriptionPort
from app.surfaces import owner
from app.surfaces.owner_crm_intent import is_explicit_owner_crm_write_intent
from app.tools.owner.crm import _crm_upsert
from app.tools.owner.types import ToolContext
from app.workers import telegram_owner
from sqlalchemy import text as sql_text

ACTOR = "880099"


@pytest.mark.parametrize(
    "text",
    [
        "Summarize this: “save Dana dana@example.com in Contacts”",
        "Summarize this: ‘save Dana dana@example.com in Contacts’",
        "Summarize this: «save Dana dana@example.com in Contacts»",
        "Summarize this: `save Dana dana@example.com in Contacts`",
        "```save Dana dana@example.com in Contacts```",
        "> save Dana dana@example.com in Contacts",
        "Don’t save Dana dana@example.com in Contacts",
        "No need to save Dana dana@example.com in Contacts",
        "Should I save Dana dana@example.com in Contacts?",
        'Summarize this email: "save Dana dana@example.com in Contacts',
    ],
)
def test_quoted_or_provider_crm_text_never_authorizes_write(text: str) -> None:
    assert not is_explicit_owner_crm_write_intent(text)


def test_current_owner_crm_write_still_authorizes_normal_english_and_hebrew() -> None:
    assert is_explicit_owner_crm_write_intent("Save Dana dana@example.com in Contacts")
    assert is_explicit_owner_crm_write_intent("תרשמי את דנה dana@example.com באנשי קשר")


def test_crm_handler_rejects_quoted_provider_instruction_before_idempotency() -> None:
    init_db()
    db = get_session_factory()()
    sheets = FakeSheetsPort()
    try:
        ctx = ToolContext(
            store=LeadStore(db),
            brain=BrainStore(db),
            settings=Settings(_env_file=None),
            principal=Principal.owner(source="telegram", actor_id=ACTOR),
            embedding_port=FakeEmbeddingPort(),
            sheets=sheets,
            owner_text="Summarize this email: “save Dana dana@example.com in Contacts”",
            source_ref="surface-quoted-crm",
        )
        result = _crm_upsert(
            ctx,
            {"name": "Dana", "phone": "", "email": "dana@example.com"},
        )
        assert not result.ok
        assert sheets.owner_operations == []
    finally:
        db.close()


def _claim(event_id: str) -> None:
    init_db()
    db = get_session_factory()()
    try:
        assert LeadStore(db).claim_webhook(
            provider="telegram", provider_event_id=event_id, channel="telegram"
        )
        db.commit()
    finally:
        db.close()


@pytest.mark.asyncio
async def test_kill_switch_worker_stops_before_voice_image_or_adapters(monkeypatch) -> None:
    event_id = "surface-stop-worker"
    _claim(event_id)
    settings = Settings(_env_file=None, kill_switch=True, telegram_owner_user_ids=ACTOR)
    monkeypatch.setattr(telegram_owner, "get_settings", lambda: settings)

    def forbidden(*_args, **_kwargs):
        pytest.fail("kill switch started active owner work")

    # The worker imports STT locally, so patch the source binding it resolves at
    # runtime rather than an absent worker module attribute.
    monkeypatch.setattr("app.api.telegram._transcribe_telegram_voice", forbidden)
    monkeypatch.setattr(telegram_owner, "_see_telegram_photo", forbidden)
    monkeypatch.setattr(telegram_owner, "build_sheets_port", forbidden)
    monkeypatch.setattr(telegram_owner, "build_contacts_crm", forbidden)
    monkeypatch.setattr(telegram_owner, "build_gmail_port", forbidden)
    port = RecordingMessagePort()

    await telegram_owner.process_telegram_owner_update(
        item={"id": event_id, "from": ACTOR, "chat_id": ACTOR, "text": "stop"},
        envelope_kind="audio",
        voice_file_id="voice-file",
        photo_file_id="photo-file",
        port=port,
        transcribe_port=FakeTranscriptionPort("should not run"),
    )
    assert port.sent == []
    db = get_session_factory()()
    try:
        assert (
            LeadStore(db).get_webhook(provider="telegram", provider_event_id=event_id).status
            == "processed"
        )
    finally:
        db.close()


@pytest.mark.asyncio
async def test_worker_drain_after_reply_does_not_send_second_timeout(monkeypatch) -> None:
    event_id = "surface-slow-learning"
    _claim(event_id)
    settings = Settings(
        _env_file=None, owner_turn_timeout_seconds=0.02, telegram_owner_user_ids=ACTOR
    )
    monkeypatch.setattr(telegram_owner, "get_settings", lambda: settings)
    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", 0)
    monkeypatch.setattr(telegram_owner, "build_sheets_port", lambda _s: SimpleNamespace())
    monkeypatch.setattr(telegram_owner, "build_contacts_crm", lambda _s, _p: SimpleNamespace())
    entered = asyncio.Event()

    async def slow_turn(**kwargs):
        entered.set()
        await kwargs["port"].send(
            telegram_owner.outbound_reply(
                kwargs["item"], text="accepted", channel=telegram_owner.Channel.TELEGRAM
            )
        )
        await asyncio.sleep(0.06)
        return owner.OwnerTurnResult(processed=True, sent=True, last_reply="accepted")

    monkeypatch.setattr(telegram_owner, "run_owner_loop", slow_turn)
    port = RecordingMessagePort()
    task = asyncio.create_task(
        telegram_owner.process_telegram_owner_update(
            item={"id": event_id, "from": ACTOR, "chat_id": ACTOR, "text": "check"},
            envelope_kind="text",
            voice_file_id=None,
            port=port,
            transcribe_port=FakeTranscriptionPort("unused"),
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    assert len(port.sent) == 1
    await asyncio.sleep(0.06)
    assert not task.done(), "worker must still be draining slow learning"
    await task
    assert len(port.sent) == 1
    assert "תם הזמן" not in port.sent[0].text


@pytest.mark.asyncio
async def test_worker_learning_exception_after_reply_does_not_emit_failure(monkeypatch) -> None:
    event_id = "surface-learning-error"
    _claim(event_id)
    settings = Settings(_env_file=None, telegram_owner_user_ids=ACTOR)
    monkeypatch.setattr(telegram_owner, "get_settings", lambda: settings)
    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", 0)
    monkeypatch.setattr(telegram_owner, "build_sheets_port", lambda _s: SimpleNamespace())
    monkeypatch.setattr(telegram_owner, "build_contacts_crm", lambda _s, _p: SimpleNamespace())
    monkeypatch.setattr(owner, "_talk_with_optional_agent", lambda **_kwargs: ("accepted", False))

    def learning_error(**_kwargs):
        raise RuntimeError("learning failed")

    port = RecordingMessagePort()
    await telegram_owner.process_telegram_owner_update(
        item={"id": event_id, "from": ACTOR, "chat_id": ACTOR, "text": "check"},
        envelope_kind="text",
        voice_file_id=None,
        port=port,
        transcribe_port=FakeTranscriptionPort("unused"),
    )
    assert len(port.sent) == 1
    assert "לא עברה" not in port.sent[0].text


@pytest.mark.asyncio
async def test_worker_cancellation_drains_owner_task_before_closing_db(monkeypatch) -> None:
    event_id = "surface-cancel-drain"
    _claim(event_id)
    settings = Settings(_env_file=None, telegram_owner_user_ids=ACTOR)
    monkeypatch.setattr(telegram_owner, "get_settings", lambda: settings)
    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", 0)
    entered = asyncio.Event()
    drained = asyncio.Event()

    async def slow_turn(**kwargs):
        entered.set()
        kwargs["delivery_state"]["sent"] = True
        kwargs["store"].mark_webhook(provider="telegram", provider_event_id=event_id, status="sent")
        await asyncio.sleep(0.03)
        assert kwargs["store"].session.execute(sql_text("SELECT 1")).scalar_one() == 1
        drained.set()

    monkeypatch.setattr(telegram_owner, "run_owner_loop", slow_turn)
    real_factory = telegram_owner.get_session_factory
    created_sessions: list[object] = []

    class SpySession:
        def __init__(self, wrapped) -> None:
            self._wrapped = wrapped
            self.closed = False

        def close(self) -> None:
            self.closed = True
            self._wrapped.close()

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

    def factory():
        wrapped = real_factory()()
        spy = SpySession(wrapped)
        created_sessions.append(spy)
        return spy

    monkeypatch.setattr(telegram_owner, "get_session_factory", lambda: factory)
    task = asyncio.create_task(
        telegram_owner.process_telegram_owner_update(
            item={"id": event_id, "from": ACTOR, "chat_id": ACTOR, "text": "check"},
            envelope_kind="text",
            voice_file_id=None,
            port=RecordingMessagePort(),
            transcribe_port=FakeTranscriptionPort("unused"),
        )
    )
    await asyncio.wait_for(entered.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert drained.is_set()
    assert created_sessions and created_sessions[0].closed
    verify = get_session_factory()()
    try:
        assert (
            LeadStore(verify).get_webhook(provider="telegram", provider_event_id=event_id).status
            == "sent"
        )
    finally:
        verify.close()


@pytest.mark.asyncio
async def test_owner_turn_stage_covers_full_async_owner_loop(monkeypatch) -> None:
    observed: list[float] = []

    @contextmanager
    def capture(stage: str, **_kwargs):
        started = monotonic()
        yield
        if stage == "owner_turn":
            observed.append(monotonic() - started)

    async def delayed_turn(**_kwargs):
        await asyncio.sleep(0.02)
        return "done"

    monkeypatch.setattr(telegram_owner, "owner_stage", capture)
    monkeypatch.setattr(telegram_owner, "run_owner_loop", delayed_turn)
    result = await telegram_owner._run_owner_turn_timed(item={"id": "stage-1"})
    assert result == "done"
    assert observed and observed[0] >= 0.015


@pytest.mark.asyncio
async def test_accepted_send_survives_post_send_persistence_error(monkeypatch) -> None:
    from app.domain.events import Channel

    event_id = "surface-post-send-persistence"
    _claim(event_id)
    db = get_session_factory()()
    store = LeadStore(db)
    port = RecordingMessagePort()
    monkeypatch.setattr(
        LeadStore,
        "mark_webhook",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("db closed")),
    )
    try:
        result = await owner.run_owner_loop(
            item={"id": event_id, "from": ACTOR, "chat_id": ACTOR, "text": "hello"},
            store=store,
            port=port,
            settings=Settings(_env_file=None, telegram_owner_user_ids=ACTOR),
            crm=SimpleNamespace(),
            owner_ids={ACTOR},
            channel=Channel.TELEGRAM,
            talk=lambda **_kwargs: ("accepted", False),
        )
    finally:
        db.close()
    assert result.sent is True
    assert len(port.sent) == 1


@pytest.mark.asyncio
async def test_v2_owner_turn_never_starts_passive_learning(monkeypatch) -> None:
    event_id = "surface-real-learning-thread"
    _claim(event_id)
    settings = Settings(
        _env_file=None,
        owner_turn_timeout_seconds=0.02,
        telegram_owner_user_ids=ACTOR,
    )
    monkeypatch.setattr(telegram_owner, "get_settings", lambda: settings)
    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", 0)
    monkeypatch.setattr(telegram_owner, "build_sheets_port", lambda _s: SimpleNamespace())
    monkeypatch.setattr(telegram_owner, "build_contacts_crm", lambda _s, _p: SimpleNamespace())
    monkeypatch.setattr(owner, "_talk_with_optional_agent", lambda **_kwargs: ("accepted", False))

    def forbidden_learning(**_kwargs):
        raise AssertionError("v2 owner entrypoints must not passively learn")

    port = RecordingMessagePort()
    task = asyncio.create_task(
        telegram_owner.process_telegram_owner_update(
            item={"id": event_id, "from": ACTOR, "chat_id": ACTOR, "text": "check"},
            envelope_kind="text",
            voice_file_id=None,
            port=port,
            transcribe_port=FakeTranscriptionPort("unused"),
        )
    )
    await task
    assert len(port.sent) == 1


@pytest.mark.asyncio
async def test_typing_cleanup_error_after_accepted_reply_does_not_send_failure(monkeypatch) -> None:
    event_id = "surface-typing-error"
    _claim(event_id)
    settings = Settings(_env_file=None, telegram_owner_user_ids=ACTOR)
    monkeypatch.setattr(telegram_owner, "get_settings", lambda: settings)
    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", 0)
    monkeypatch.setattr(telegram_owner, "build_sheets_port", lambda _s: SimpleNamespace())
    monkeypatch.setattr(telegram_owner, "build_contacts_crm", lambda _s, _p: SimpleNamespace())
    monkeypatch.setattr(owner, "_talk_with_optional_agent", lambda **_kwargs: ("accepted", False))

    async def typing_error(**_kwargs):
        raise ValueError("typing cleanup")

    monkeypatch.setattr(telegram_owner, "_renew_typing", typing_error)
    port = RecordingMessagePort()
    await telegram_owner.process_telegram_owner_update(
        item={"id": event_id, "from": ACTOR, "chat_id": ACTOR, "text": "check"},
        envelope_kind="text",
        voice_file_id=None,
        port=port,
        transcribe_port=FakeTranscriptionPort("unused"),
    )
    assert len(port.sent) == 1
    assert "לא עברה" not in port.sent[0].text


@pytest.mark.asyncio
async def test_preloop_adapter_builder_failure_sends_one_failure(monkeypatch) -> None:
    """A failure before the owner loop still has a safe delivery state."""
    event_id = "surface-preloop-builder-error"
    _claim(event_id)
    settings = Settings(_env_file=None, telegram_owner_user_ids=ACTOR)
    monkeypatch.setattr(telegram_owner, "get_settings", lambda: settings)
    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", 0)

    def builder_failure(_settings):
        raise RuntimeError("adapter construction failed")

    monkeypatch.setattr(telegram_owner, "build_sheets_port", builder_failure)
    port = RecordingMessagePort()
    await telegram_owner.process_telegram_owner_update(
        item={"id": event_id, "from": ACTOR, "chat_id": ACTOR, "text": "check"},
        envelope_kind="text",
        voice_file_id=None,
        port=port,
        transcribe_port=FakeTranscriptionPort("unused"),
    )
    assert len(port.sent) == 1
    assert "לא עברה" in port.sent[0].text
