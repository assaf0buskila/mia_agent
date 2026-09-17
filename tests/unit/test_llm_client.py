"""A truncation must stay a truncation, on both transports.

Two live-shaped defects, one contract. The Responses adapter already strips a
`status=incomplete / max_output_tokens` body to no prose and no tool calls, so the
caller can fail closed. But:

1. `LlmModelChain` saw that stripped shape as "no text and no tools", called it
   `empty_reply`, and advanced to the next model. The one `finish_reason` that names
   truncation never reached a caller on the primary transport, and the chain spent its
   fallback rung on a token budget the fallback would hit in exactly the same way.
2. The Chat/Gemini transport applied no strip at all. A truncated body returns partial
   prose plus a function call whose argument string was cut mid-JSON --
   `parse_tool_arguments` normalises that to `{}` without raising, so the call looks
   well formed. `run_owner_agent`'s guard read `truncated() and not response.text`, so
   the partial prose carried a half-generated intent straight into tool dispatch.

The chain guard has to come first: stripping the chat transport without it would only
have exported defect 1 to the fallback provider.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.graph import owner_agent
from app.graph.owner_agent import run_owner_agent
from app.integrations.llm_client import (
    GEMINI_CHAT_URL,
    OPENAI_RESPONSES_URL,
    LlmClient,
    LlmModelChain,
)
from app.tools.registries.owner_tools import ToolContext

TRUNCATED_TOOL = "daily_brief"
CUT_ARGUMENTS = '{"day": "2026-09-1'


def _responses_truncation() -> dict:
    """What OpenAI Responses returns when reasoning eats the whole output budget.

    The output still carries a function call that *looks* finished; its argument
    string is cut mid-JSON.
    """
    return {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": TRUNCATED_TOOL,
                "arguments": CUT_ARGUMENTS,
            }
        ],
        "usage": {"input_tokens": 120, "output_tokens": 180},
    }


def _chat_truncation() -> dict:
    """The same event on the Chat/Gemini transport: partial prose AND a partial call."""
    return {
        "choices": [
            {
                "finish_reason": "length",
                "message": {
                    "role": "assistant",
                    "content": "רגע, אני מושכת את הסיכום היומי ו",
                    "tool_calls": [
                        {
                            "id": "call_g1",
                            "type": "function",
                            "function": {
                                "name": TRUNCATED_TOOL,
                                "arguments": CUT_ARGUMENTS,
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 120, "completion_tokens": 180},
    }


def _chat_stop(text: str) -> dict:
    return {
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": text}}
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
    }


class _Script:
    """Replays one body per request and records how many rungs were actually called."""

    def __init__(self, bodies: list[dict]) -> None:
        self._bodies = list(bodies)
        self.requests: list[dict] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content.decode("utf-8")))
        index = min(len(self.requests) - 1, len(self._bodies) - 1)
        return httpx.Response(200, json=self._bodies[index])


def test_a_truncation_is_returned_not_spent_on_the_fallback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The chain must hand the truncation back, and must not call the second model.

    Before the fix this returned the *fallback's* answer with finish_reason "stop",
    recorded `primary:empty_reply`, and the caller had no way to learn that the real
    event was an exhausted output budget.
    """
    script = _Script([_responses_truncation(), _chat_stop("fallback answered")])
    http = httpx.Client(transport=httpx.MockTransport(script.handle))
    chain = LlmModelChain(
        [
            LlmClient(api_key="k", model="primary", url=OPENAI_RESPONSES_URL, client=http),
            LlmClient(api_key="k2", model="fallback", url=GEMINI_CHAT_URL, client=http),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="app.integrations.llm_client"):
        response = chain.complete(messages=[{"role": "user", "content": "מה יש היום"}])

    assert response.finish_reason == "length"
    assert response.truncated() is True
    assert len(script.requests) == 1, "the fallback rung must never have been called"
    assert chain.last_model == "primary"
    assert chain.errors == [], "a truncation is not a model failure"
    assert any(
        "llm chain returned truncation reason=truncated" in record.getMessage()
        for record in caplog.records
    )


def test_chat_transport_truncation_never_exposes_a_partial_function_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Gemini's truncation carries readable prose and a call whose JSON is cut.

    Before the fix `text` held the partial sentence and `tool_calls` held one
    `daily_brief` call whose broken arguments `parse_tool_arguments` had quietly
    normalised to `{}` -- indistinguishable from a real no-argument call.
    """
    script = _Script([_chat_truncation()])
    client = LlmClient(
        api_key="k",
        model="gemini-fallback",
        url=GEMINI_CHAT_URL,
        client=httpx.Client(transport=httpx.MockTransport(script.handle)),
    )

    with caplog.at_level(logging.WARNING, logger="app.integrations.llm_client"):
        response = client.complete(messages=[{"role": "user", "content": "מה יש היום"}])

    assert response.truncated() is True
    assert response.text == ""
    assert response.tool_calls == ()
    assert response.tokens_out == 180, "usage must survive the strip"
    assert any(
        "llm chat reply stripped reason=truncated" in record.getMessage()
        for record in caplog.records
    )


def test_a_truncated_owner_turn_reports_truncated_and_runs_no_tool() -> None:
    """The owner loop must stop at a truncation instead of dispatching half an intent.

    Before the fix the chat transport's partial prose made
    `response.truncated() and not response.text` false, and the loop executed the
    `daily_brief` call built from a cut-off argument string.
    """
    init_db()
    db = get_session_factory()()
    executed: list[str] = []

    def _record(
        name: str,
        arguments: dict,
        tool_ctx: object,
        *,
        deadline_at: float | None = None,
    ) -> object:
        """Record the dispatch and answer plausibly, so the assertion is what fails."""
        from app.tools.registries.owner_tools import ToolResult

        executed.append(name)
        return ToolResult(ok=True, text="ok")

    original = owner_agent._run_tool_with_timeout
    try:
        ctx = ToolContext(
            principal=Principal.owner(source="telegram", actor_id="1"),
            store=LeadStore(db),
            brain=BrainStore(db),
            settings=Settings(_env_file=None),
            embedding_port=FakeEmbeddingPort(),
        )
        script = _Script([_chat_truncation()])
        client = LlmClient(
            api_key="k",
            model="gemini-fallback",
            url=GEMINI_CHAT_URL,
            client=httpx.Client(transport=httpx.MockTransport(script.handle)),
        )
        owner_agent._run_tool_with_timeout = _record  # type: ignore[assignment]
        outcome = run_owner_agent(client=client, ctx=ctx, owner_message="מה יש היום")
    finally:
        owner_agent._run_tool_with_timeout = original  # type: ignore[assignment]
        db.close()

    assert outcome.completion == "truncated"
    assert outcome.completed is False
    assert outcome.text == "", "partial prose must never reach the owner as an answer"
    assert executed == []
    assert outcome.tools_used == ()
