"""OS-RUNTIME F4: the Mia owner turn no longer runs as one long worker transaction.

``run_owner_loop`` used to INSERT the incoming canonical event, then run the whole agent (LLM
calls, Apps Script tool calls, the proposal ApprovalRow INSERT, Telegram sends) before the worker
committed once at the end. So the approval card went out before its row existed for anyone else,
and an uncommitted write transaction was held across every external call.

The turn now commits at fixed points: before the agent, before every model call, before the
outbound send loop and at the end of the loop. A file-backed SQLite store or a disposable
PostgreSQL schema gives every probe its own connection, so it sees only committed state.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from app.api.owner import OwnerTurnResult
from app.db.models import ApprovalRow, CanonicalEventRow
from app.db.store import LeadStore
from app.domain.owner import brain
from app.integrations.base import RecordingMessagePort
from app.integrations.llm_client import LlmError, LlmResponse, ToolCall
from app.integrations.transcribe import FakeTranscriptionPort
from app.services import assaf_os as port
from app.services.os_ledger import api as ledger_api
from app.surfaces.owner import run_owner_loop
from app.workers import telegram_owner
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from tests.services.os_runtime_support import (
    foreign_write_xids,
    open_factory,
    visible_owner_actions,
)
from tests.unit import test_assaf_os as assaf
from tests.unit import test_website_lead_approval as leads

OWNER = leads.OWNER
PROPOSE_ARGUMENTS = {
    "request_id": assaf.REQUEST,
    "entity": "tasks",
    "record_id": None,
    "expected_revision": 0,
    "input_json": json.dumps({"title": "Follow up", "status": "לביצוע"}, ensure_ascii=False),
}


@pytest.fixture(params=["sqlite", "postgres"])
def factory(request, tmp_path):
    built, close = open_factory(request.param, tmp_path, "f4")
    yield built
    close()


def _claim(factory, event_id: str) -> None:
    with factory() as session:
        assert LeadStore(session).claim_webhook(
            provider="telegram", provider_event_id=event_id, channel="telegram",
            envelope_kind="text",
        )
        session.commit()


class ScriptedModel:
    """A model that proposes one Assaf OS task, then answers. It reports what it saw per call."""

    last_model = "scripted-test-model"

    def __init__(self, observe, *, fail_second: bool = False) -> None:
        self.observe = observe
        self.fail_second = fail_second
        self.calls = 0
        self.seen: list[dict] = []

    def enabled(self) -> bool:
        return True

    def complete(self, **_kwargs) -> LlmResponse:
        self.calls += 1
        self.seen.append(self.observe())
        if self.calls == 1:
            encoded = json.dumps(PROPOSE_ARGUMENTS)
            call = ToolCall("propose-1", "assaf_os_propose", dict(PROPOSE_ARGUMENTS), encoded)
            return LlmResponse(
                "", (call,), "tool_calls", "", 1, 1,
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": call.call_id, "type": "function",
                        "function": {"name": call.name, "arguments": encoded},
                    }],
                },
            )
        if self.fail_second:
            raise LlmError("synthetic provider failure")
        return LlmResponse(
            "הכנתי הצעה.", (), "stop", "", 1, 1,
            {"role": "assistant", "content": "הכנתי הצעה."},
        )


class ProbingPort(RecordingMessagePort):
    """Records what a separate connection sees at the moment of every Telegram send."""

    def __init__(self, probe) -> None:
        super().__init__()
        self._probe = probe
        self.seen: list[tuple[bool, int]] = []

    async def send(self, message) -> None:
        self.seen.append((message.reply_markup is not None, self._probe()))
        await super().send(message)


@pytest.fixture
def turn(factory, monkeypatch):
    """One owner turn on a real session: returns ``(run, model, script_calls, port, session)``."""
    session = factory()
    store = LeadStore(session)
    settings = leads._settings(
        owner_agent_model="scripted-test-model",
        openai_api_key="test-only-not-a-credential",
        memory_write_enabled=False,
    )

    def observe() -> dict:
        return {
            "in_transaction": session.in_transaction(),
            "foreign_write_xids": foreign_write_xids(factory),
        }

    model = ScriptedModel(observe)
    script_calls: list[dict] = []

    def respond(_url, raw, _budget):
        request = json.loads(json.loads(raw)["payload"])
        script_calls.append({"operation": request["operation"], **observe()})
        return assaf.signed(raw, assaf._pending(settings, request["actor_source"]), settings)

    monkeypatch.setattr(brain, "build_agent_client", lambda _settings: model)
    monkeypatch.setattr(port, "_post", respond)
    monkeypatch.setattr(ledger_api, "core_authority_state", lambda _settings: "shadow")
    out = ProbingPort(lambda: visible_owner_actions(factory, "assaf_os.approve"))
    event_id = "evt-f4-turn"
    _claim(factory, event_id)

    async def run() -> OwnerTurnResult:
        return await run_owner_loop(
            item={"id": event_id, "from": OWNER, "chat_id": OWNER, "text": "add a task: call Dana"},
            store=store, port=out, settings=settings, owner_ids={OWNER},
        )

    yield run, model, script_calls, out, session
    session.close()


async def test_no_transaction_is_open_at_any_model_call(turn):
    run, model, _script, _port, _session = turn
    result = await run()
    assert result.sent is True and model.calls == 2
    for index, seen in enumerate(model.seen, start=1):
        print(f"OS-RUNTIME F4a complete#{index} in_transaction={seen['in_transaction']}")
    assert [seen["in_transaction"] for seen in model.seen] == [False, False], (
        "a transaction is open at a complete() call"
    )


async def test_no_other_backend_holds_a_write_transaction_across_the_model_or_the_script(turn):
    run, model, script_calls, _port, _session = turn
    await run()
    assert [call["operation"] for call in script_calls] == ["propose"]
    observed = [*(("complete", s) for s in model.seen), *(("_post", s) for s in script_calls)]
    if any(seen["foreign_write_xids"] is None for _, seen in observed):
        pytest.skip("the write-transaction check needs PostgreSQL (MIA_TEST_POSTGRES_URL)")
    for label, seen in observed:
        print(f"OS-RUNTIME F4c {label} backend_xid_holders={seen['foreign_write_xids']}")
    assert [seen["foreign_write_xids"] for _, seen in observed] == [0, 0, 0], (
        "another backend holds a write transaction across a model call or the Script call"
    )
    assert all(call["in_transaction"] is False for call in script_calls)


async def test_every_card_row_is_committed_before_its_card_is_sent(turn):
    run, _model, _script, out, _session = turn
    await run()
    assert [has_keyboard for has_keyboard, _ in out.seen] == [False, True], "prose then card"
    for has_keyboard, visible in out.seen:
        print(f"OS-RUNTIME F4b send(keyboard={has_keyboard}) approval_rows_visible={visible}")
    assert [visible for _, visible in out.seen] == [1, 1], (
        "the ApprovalRow is not visible to another connection at send"
    )


async def test_the_loop_ends_with_nothing_uncommitted(turn, factory):
    run, _model, _script, out, session = turn
    result = await run()
    assert result.sent is True and len(out.sent) == 2
    assert session.in_transaction() is False
    with factory() as fresh:
        webhook = LeadStore(fresh).get_webhook(provider="telegram", provider_event_id="evt-f4-turn")
        assert webhook.status == "sent"
        events = fresh.scalars(select(CanonicalEventRow.event_type)).all()
        assert len(events) == 2, "the incoming and the outgoing canonical events are committed"


async def test_an_approval_created_before_a_later_failure_stays_committed_and_pending(
    factory, monkeypatch
):
    """The documented consequence of committing before each model call."""
    session = factory()
    settings = leads._settings(
        owner_agent_model="scripted-test-model",
        openai_api_key="test-only-not-a-credential",
        memory_write_enabled=False,
    )
    model = ScriptedModel(lambda: {}, fail_second=True)
    monkeypatch.setattr(brain, "build_agent_client", lambda _settings: model)
    monkeypatch.setattr(
        port, "_post",
        lambda _url, raw, _budget: assaf.signed(
            raw, assaf._pending(settings, json.loads(json.loads(raw)["payload"])["actor_source"]),
            settings,
        ),
    )
    monkeypatch.setattr(ledger_api, "core_authority_state", lambda _settings: "shadow")
    _claim(factory, "evt-f4-fail")
    try:
        await run_owner_loop(
            item={"id": "evt-f4-fail", "from": OWNER, "chat_id": OWNER, "text": "add a task"},
            store=LeadStore(session), port=RecordingMessagePort(), settings=settings,
            owner_ids={OWNER},
        )
    finally:
        session.close()
    assert model.calls == 2
    with factory() as fresh:
        (row,) = fresh.scalars(select(ApprovalRow)).all()
        assert row.decision == "pending" and row.resource_type == "owner_proposal"


# ---- end_transaction -------------------------------------------------------------------------


def test_end_transaction_commits_only_an_open_transaction(factory):
    from app.db.session import end_transaction

    commits: list[int] = []
    session = factory()
    real_commit = session.commit
    session.commit = lambda: (commits.append(1), real_commit())[1]
    try:
        end_transaction(session)
        assert commits == [], "no transaction: nothing to commit"
        session.add(ApprovalRow(
            channel="telegram", action="x", risk="R3", payload_hash="h", decision="pending",
            approver="", resource_type="t", resource_id="r", expires_at="2099-01-01T00:00:00",
            approval_id="apr_end_tx", proposed_parameters="{}", actor_id=OWNER,
        ))
        session.flush()
        assert session.in_transaction() is True
        end_transaction(session)
        assert commits == [1] and session.in_transaction() is False
        end_transaction(session)
        assert commits == [1], "a second call is a no-op"
    finally:
        session.close()
    with factory() as fresh:
        assert fresh.scalars(select(ApprovalRow.approval_id)).all() == ["apr_end_tx"]


def test_end_transaction_skips_a_mock_session_and_never_commits_it():
    from app.db.session import end_transaction

    session = MagicMock()
    end_transaction(session)
    session.commit.assert_not_called()


def test_end_transaction_rolls_back_and_reraises_a_database_error():
    from app.db.session import end_transaction

    session = MagicMock()
    session.in_transaction.return_value = True
    session.commit.side_effect = SQLAlchemyError("synthetic commit failure")
    with pytest.raises(SQLAlchemyError, match="synthetic commit failure"):
        end_transaction(session)
    session.rollback.assert_called_once_with()


def test_release_turn_transaction_tolerates_contexts_without_a_store():
    from app.graph.owner_agent import _release_turn_transaction

    _release_turn_transaction(SimpleNamespace())
    _release_turn_transaction(SimpleNamespace(store=None))
    _release_turn_transaction(SimpleNamespace(store=SimpleNamespace(session=None)))
    session = MagicMock()
    _release_turn_transaction(SimpleNamespace(store=SimpleNamespace(session=session)))
    session.commit.assert_not_called()  # a MagicMock session is skipped by `is True`


# ---- the worker's drain and timeout branches --------------------------------------------------

ACTOR = "770044"


def _partial_turn_row(store: LeadStore, tag: str) -> None:
    """What an unfinished turn leaves uncommitted: one proposal-shaped ApprovalRow."""
    store.session.add(ApprovalRow(
        channel="telegram", action="owner_external_write", risk="R3", payload_hash=tag,
        decision="pending", approver="", resource_type="owner_proposal", resource_id="op_" + tag,
        expires_at="2099-01-01T00:00:00", approval_id="apr_" + tag, proposed_parameters="{}",
        actor_id=ACTOR,
    ))
    store.session.flush()


@pytest.fixture
def worker(factory, monkeypatch):
    from app.core.config import Settings

    settings = Settings(
        _env_file=None, owner_turn_timeout_seconds=0.05, telegram_owner_user_ids=ACTOR
    )
    monkeypatch.setattr(telegram_owner, "get_settings", lambda: settings)
    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", 0)
    monkeypatch.setattr(telegram_owner, "get_session_factory", lambda: factory)

    async def process(event_id: str, turn) -> RecordingMessagePort:
        _claim(factory, event_id)
        monkeypatch.setattr(telegram_owner, "run_owner_loop", turn)
        out = RecordingMessagePort()
        await telegram_owner.process_telegram_owner_update(
            item={"id": event_id, "from": ACTOR, "chat_id": ACTOR, "chat_type": "private",
                  "text": "check"},
            envelope_kind="text", voice_file_id=None, port=out,
            transcribe_port=FakeTranscriptionPort("unused"),
        )
        return out

    return process


def _approval_ids(factory) -> list[str]:
    with factory() as fresh:
        return list(fresh.scalars(select(ApprovalRow.approval_id)).all())


async def test_the_drain_timeout_notice_discards_the_unfinished_turns_partial_work(
    factory, worker
):
    async def hang(**kwargs):
        _partial_turn_row(kwargs["store"], "drain")
        await asyncio.sleep(0.2)

    out = await worker("evt-f4-drain", hang)
    assert out.sent and "תם הזמן" in out.sent[0].text
    with factory() as fresh:
        assert LeadStore(fresh).get_webhook(
            provider="telegram", provider_event_id="evt-f4-drain"
        ).status == "sent"
    ids = _approval_ids(factory)
    print(f"OS-RUNTIME F4d drain_timeout committed_partial_rows={len(ids)}")
    assert ids == [], "the unfinished turn's partial write was committed with the hang notice"


async def test_a_timeout_error_notice_discards_the_unfinished_turns_partial_work(
    factory, worker
):
    async def raises(**kwargs):
        _partial_turn_row(kwargs["store"], "timeout")
        raise TimeoutError

    out = await worker("evt-f4-timeout", raises)
    assert out.sent and "תם הזמן" in out.sent[0].text
    ids = _approval_ids(factory)
    print(f"OS-RUNTIME F4d timeout_error committed_partial_rows={len(ids)}")
    assert ids == [], "the unfinished turn's partial write was committed with the hang notice"


async def test_a_failed_turn_notice_discards_the_unfinished_turns_partial_work(factory, worker):
    async def fails(**kwargs):
        _partial_turn_row(kwargs["store"], "failed")
        raise RuntimeError("turn failed")

    out = await worker("evt-f4-failed", fails)
    assert out.sent and out.sent[0].text.startswith("הבדיקה לא עברה כרגע.")
    with factory() as fresh:
        assert LeadStore(fresh).get_webhook(
            provider="telegram", provider_event_id="evt-f4-failed"
        ).status == "sent"
    ids = _approval_ids(factory)
    print(f"OS-RUNTIME F4d failed_turn committed_partial_rows={len(ids)}")
    assert ids == [], "the unfinished turn's partial write was committed with the failure notice"


async def test_a_delivered_reply_keeps_the_turns_work_even_when_it_drained_late(factory, worker):
    async def delivers_late(**kwargs):
        _partial_turn_row(kwargs["store"], "delivered")
        kwargs["delivery_state"]["sent"] = True
        await asyncio.sleep(0.2)
        return OwnerTurnResult(processed=True, sent=True, last_reply="done")

    out = await worker("evt-f4-delivered", delivers_late)
    assert out.sent == [], "no timeout notice after a delivered answer"
    assert _approval_ids(factory) == ["apr_delivered"]
