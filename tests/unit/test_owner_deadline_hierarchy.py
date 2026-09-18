"""TG-DL: the owner deadline hierarchy -- `child_call_timeout` and its call sites.

RC1/RC2/RC3 (see app/workers/telegram_owner.py, app/graph/owner_agent.py,
app/integrations/llm_client.py) shipped because no single test proved the
arithmetic that bounds one expensive child call inside the owner turn
deadline. This file is that proof, plus the two-`None`s trap the frozen
`app/core/deadlines.py` interface warns about: `child_call_timeout` returning
`None` means "do not start this call"; `timeout=None` passed to
`LlmClient.complete` means "use the client default" -- i.e. GO. The two must
never be confused.
"""

from __future__ import annotations

import asyncio
import json
from time import monotonic

import pytest
from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.core.deadlines import child_call_timeout, remaining_seconds
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.graph import owner_agent
from app.integrations.base import RecordingMessagePort
from app.integrations.llm_client import LlmError, LlmModelChain, LlmResponse, ToolCall
from app.integrations.transcribe import FakeTranscriptionPort
from app.tools.registries.owner_tools import ToolContext, ToolResult
from app.workers import telegram_owner

ACTOR = "770011"


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


def _ctx(settings: Settings, *, owner_text: str = "") -> ToolContext:
    init_db()
    db = get_session_factory()()
    return ToolContext(
        principal=Principal.owner(source="telegram", actor_id=ACTOR),
        store=LeadStore(db),
        brain=BrainStore(db),
        settings=settings,
        embedding_port=FakeEmbeddingPort(),
        source_ref="telegram:tgdl",
        owner_text=owner_text,
    )


# ---------------------------------------------------------------------------
# 1. child_call_timeout / remaining_seconds arithmetic
# ---------------------------------------------------------------------------


def test_remaining_seconds_is_none_only_when_unbounded() -> None:
    assert remaining_seconds(None) is None
    now = monotonic()
    value = remaining_seconds(now + 5.0)
    assert value is not None
    assert 4.5 <= value <= 5.0


def test_child_call_timeout_clamps_to_configured_when_plenty_of_time_left() -> None:
    result = child_call_timeout(
        deadline_at=monotonic() + 1000.0, configured=5.0, reserve=0.0, minimum=1.0
    )
    assert result == 5.0


def test_child_call_timeout_clamps_to_usable_when_configured_is_larger() -> None:
    result = child_call_timeout(
        deadline_at=monotonic() + 3.0, configured=100.0, reserve=0.0, minimum=1.0
    )
    assert result is not None
    assert 2.5 <= result <= 3.0


def test_child_call_timeout_returns_none_below_minimum() -> None:
    result = child_call_timeout(
        deadline_at=monotonic() + 1.0, configured=100.0, reserve=0.0, minimum=5.0
    )
    assert result is None


def test_child_call_timeout_reserve_eats_into_usable_budget() -> None:
    result = child_call_timeout(
        deadline_at=monotonic() + 3.0, configured=100.0, reserve=2.5, minimum=0.1
    )
    assert result is not None
    assert 0.1 <= result <= 0.5


def test_child_call_timeout_returns_configured_when_parent_unbounded() -> None:
    # An unbounded parent still bounds the child -- reserve/minimum are irrelevant here.
    result = child_call_timeout(deadline_at=None, configured=7.0, reserve=999.0, minimum=999.0)
    assert result == 7.0


def test_child_call_timeout_rejects_non_positive_configured() -> None:
    with pytest.raises(ValueError):
        child_call_timeout(deadline_at=None, configured=0.0, minimum=1.0)
    with pytest.raises(ValueError):
        child_call_timeout(deadline_at=monotonic() + 5.0, configured=-1.0, minimum=1.0)


# ---------------------------------------------------------------------------
# 2 & 3. The two-`None`s trap: a refused child call is never started, and its
# `None` never reaches a `timeout=` kwarg as an accidental "go ahead".
# ---------------------------------------------------------------------------


class _NeverCallClient:
    """A model client that fails the test the moment it is actually invoked."""

    model = "never-call"

    def enabled(self) -> bool:
        return True

    def complete(self, **kwargs):  # noqa: ANN003 - test double
        raise AssertionError(
            "the model must never be called once child_call_timeout refused the call"
        )


def test_model_call_not_started_when_remaining_parent_time_insufficient() -> None:
    settings = Settings(
        _env_file=None,
        owner_final_reserve_seconds=5.0,
        owner_min_model_seconds=6.0,
        owner_model_attempt_timeout_seconds=20.0,
    )
    ctx = _ctx(settings, owner_text="בדיקה")
    # usable = 3 - reserve(5) < minimum(6) -> child_call_timeout must return None,
    # and that None must never be forwarded to client.complete as `timeout=None`
    # (which LlmClient reads as "use the client default", i.e. an unbounded GO).
    deadline_at = monotonic() + 3.0
    outcome = owner_agent.run_owner_agent(
        client=_NeverCallClient(),
        ctx=ctx,
        owner_message="בדיקה",
        deadline_at=deadline_at,
    )
    assert outcome.completed is False
    assert outcome.completion == "deadline_exceeded"


def test_tool_call_not_started_when_remaining_parent_time_insufficient(monkeypatch) -> None:
    settings = Settings(
        _env_file=None, owner_final_reserve_seconds=5.0, owner_min_tool_seconds=2.0
    )
    ctx = _ctx(settings)
    started: list[str] = []
    monkeypatch.setattr(
        owner_agent,
        "execute_tool",
        lambda *_a, **_k: started.append("started") or ToolResult(ok=True, text="x"),
    )
    # usable = 3 - reserve(5) < minimum(2) -> refused before the tool thread starts.
    deadline_at = monotonic() + 3.0
    result = owner_agent._run_tool_with_timeout(
        "crm_search", {}, ctx, deadline_at=deadline_at
    )
    assert started == [], "the underlying tool must never actually run"
    assert result.ok is False
    assert result.text == owner_agent.TOOL_DEADLINE_REPLY


def test_timeout_stage_reason_codes_are_bounded_and_leak_no_content(caplog) -> None:
    settings = Settings(
        _env_file=None,
        owner_final_reserve_seconds=5.0,
        owner_min_model_seconds=6.0,
        owner_model_attempt_timeout_seconds=20.0,
    )
    secret = "SECRET_OWNER_MESSAGE_MUST_NOT_LEAK"
    ctx = _ctx(settings, owner_text=secret)
    deadline_at = monotonic() + 3.0
    with caplog.at_level("INFO", logger="mia.owner_timing"):
        outcome = owner_agent.run_owner_agent(
            client=_NeverCallClient(),
            ctx=ctx,
            owner_message=secret,
            deadline_at=deadline_at,
        )
    assert outcome.completion == "deadline_exceeded"
    rendered = " ".join(record.getMessage() for record in caplog.records)
    assert "timeout_stage=" in rendered
    assert secret not in rendered


# ---------------------------------------------------------------------------
# 4. A slow/failed primary leaves a usable bounded fallback budget.
# ---------------------------------------------------------------------------


def _tool_call_response(call_id: str, name: str, arguments: dict) -> LlmResponse:
    raw_arguments = json.dumps(arguments)
    return LlmResponse(
        text="",
        tool_calls=(
            ToolCall(
                call_id=call_id, name=name, arguments=arguments, raw_arguments=raw_arguments
            ),
        ),
        finish_reason="tool_calls",
        refusal="",
        tokens_in=5,
        tokens_out=5,
        raw_message={
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": raw_arguments},
                }
            ],
        },
    )


def _text_response(text: str) -> LlmResponse:
    return LlmResponse(
        text=text,
        tool_calls=(),
        finish_reason="stop",
        refusal="",
        tokens_in=5,
        tokens_out=5,
        raw_message={"role": "assistant", "content": text},
    )


class _ScriptedChainClient:
    """A model rung whose responses (or raised `LlmError`s) are scripted in order."""

    def __init__(self, model: str, provider: str, script: list) -> None:
        self.model = model
        self.provider = provider
        self._script = list(script)
        self.calls: list[dict] = []

    def enabled(self) -> bool:
        return True

    def complete(self, **kwargs):  # noqa: ANN003 - test double
        self.calls.append(kwargs)
        if not self._script:
            raise AssertionError(f"{self.model} script exhausted")
        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def test_slow_or_failed_primary_leaves_a_usable_bounded_fallback_budget() -> None:
    primary = _ScriptedChainClient(
        "primary", "providerA", [LlmError("llm request failed: HTTP 500")]
    )
    fallback = _ScriptedChainClient("fallback", "providerB", [_text_response("שלום")])
    chain = LlmModelChain([primary, fallback])

    # `attempt_timeout` (5.0) is deliberately far below the remaining chain
    # budget (~18.0) so the assertion below only passes if the per-rung cap is
    # actually applied -- a chain that shares one deadline across every rung
    # (the pre-fix bug) would hand the fallback something close to 18.0, not 5.0.
    response = chain.complete(
        messages=[{"role": "user", "content": "hi"}],
        timeout=18.0,
        attempt_timeout=5.0,
    )

    assert response.text == "שלום"
    assert len(primary.calls) == 1
    assert len(fallback.calls) == 1
    fallback_timeout = fallback.calls[0]["timeout"]
    assert fallback_timeout is not None
    assert 0 < fallback_timeout <= 5.0


# ---------------------------------------------------------------------------
# 5. Tool + synthesis complete inside the parent budget.
# ---------------------------------------------------------------------------


def test_tool_then_synthesis_completes_inside_parent_budget(monkeypatch) -> None:
    calls: list[str] = []

    def fake_tool(name, arguments, ctx, **kwargs):  # noqa: ANN001
        calls.append(name)
        return ToolResult(ok=True, text="Contact row")

    monkeypatch.setattr(owner_agent, "_run_tool_with_timeout", fake_tool)
    client = _ScriptedChainClient(
        "single",
        "providerA",
        [
            _tool_call_response("c1", "crm_search", {"query": "Dana"}),
            _text_response("Found Dana."),
        ],
    )
    settings = Settings(_env_file=None)
    ctx = _ctx(settings)
    outcome = owner_agent.run_owner_agent(
        client=client,
        ctx=ctx,
        owner_message="בדוק CRM",
        deadline_at=monotonic() + 30.0,
    )
    assert outcome.completed is True
    assert outcome.text == "Found Dana."
    assert calls == ["crm_search"]


# ---------------------------------------------------------------------------
# 6. Fallback reuses already-collected tool evidence and never replays a
# side effect, even when the repeated call arrives from a different rung.
# ---------------------------------------------------------------------------


def test_fallback_does_not_replay_an_already_completed_tool(monkeypatch) -> None:
    calls: list[str] = []

    def fake_tool(name, arguments, ctx, **kwargs):  # noqa: ANN001
        calls.append(name)
        return ToolResult(ok=True, text="Contact row")

    monkeypatch.setattr(owner_agent, "_run_tool_with_timeout", fake_tool)

    primary = _ScriptedChainClient(
        "primary",
        "providerA",
        [
            _tool_call_response("c1", "crm_search", {"query": "Dana"}),
            LlmError("llm request failed: HTTP 500"),
        ],
    )
    # The fallback rung asks for the EXACT same tool + arguments again -- the
    # existing seen_calls/completed_call_results dedupe in run_owner_agent must
    # answer from the cached result instead of running the tool a second time,
    # regardless of which rung produced the repeated request.
    fallback = _ScriptedChainClient(
        "fallback",
        "providerB",
        [
            _tool_call_response("c2", "crm_search", {"query": "Dana"}),
            _text_response("סיימתי."),
        ],
    )
    chain = LlmModelChain([primary, fallback])
    settings = Settings(_env_file=None)
    ctx = _ctx(settings)

    outcome = owner_agent.run_owner_agent(
        client=chain,
        ctx=ctx,
        owner_message="בדוק CRM",
    )

    assert outcome.completed is True
    assert outcome.text == "סיימתי."
    assert calls == ["crm_search"], "the tool must run exactly once, never replayed"


# ---------------------------------------------------------------------------
# 7 & 8. Worker-level delivery guarantees: exactly one timeout reply, and a
# late completion during drain never creates a duplicate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_genuine_owner_timeout_emits_exactly_one_reply(monkeypatch) -> None:
    event_id = "tgdl-exactly-one-timeout"
    _claim(event_id)
    settings = Settings(
        _env_file=None, owner_turn_timeout_seconds=0.05, telegram_owner_user_ids=ACTOR
    )
    monkeypatch.setattr(telegram_owner, "get_settings", lambda: settings)
    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", 0)

    async def hang(**_kwargs):
        await asyncio.sleep(0.2)
        return None

    monkeypatch.setattr(telegram_owner, "run_owner_loop", hang)
    port = RecordingMessagePort()

    await telegram_owner.process_telegram_owner_update(
        item={"id": event_id, "from": ACTOR, "chat_id": ACTOR, "text": "?"},
        envelope_kind="text",
        voice_file_id=None,
        port=port,
        transcribe_port=FakeTranscriptionPort("unused"),
    )

    assert len(port.sent) == 1
    assert "תם הזמן" in port.sent[0].text


@pytest.mark.asyncio
async def test_late_completion_during_drain_never_duplicates_the_reply(monkeypatch) -> None:
    from app.api.owner import OwnerTurnResult

    event_id = "tgdl-no-duplicate-on-drain"
    _claim(event_id)
    settings = Settings(
        _env_file=None, owner_turn_timeout_seconds=0.02, telegram_owner_user_ids=ACTOR
    )
    monkeypatch.setattr(telegram_owner, "get_settings", lambda: settings)
    monkeypatch.setattr(telegram_owner, "COALESCE_WAIT_S", 0)

    async def slow_but_delivers(**kwargs):
        await kwargs["port"].send(
            telegram_owner.outbound_reply(
                kwargs["item"], text="real answer", channel=Channel.TELEGRAM
            )
        )
        await asyncio.sleep(0.08)
        return OwnerTurnResult(processed=True, sent=True, last_reply="real answer")

    monkeypatch.setattr(telegram_owner, "run_owner_loop", slow_but_delivers)
    port = RecordingMessagePort()

    await telegram_owner.process_telegram_owner_update(
        item={"id": event_id, "from": ACTOR, "chat_id": ACTOR, "text": "check"},
        envelope_kind="text",
        voice_file_id=None,
        port=port,
        transcribe_port=FakeTranscriptionPort("unused"),
    )

    assert len(port.sent) == 1
    assert port.sent[0].text == "real answer"
