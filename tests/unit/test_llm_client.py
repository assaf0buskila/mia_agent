"""A truncation must stay a truncation, on both transports.

Two live-shaped defects, one contract. The Responses adapter already strips a
`status=incomplete / max_output_tokens` body to no prose and no tool calls, so the
caller can fail closed. But:

1. `LlmModelChain` saw that stripped shape as "no text and no tools", called it
   `empty_reply`, and advanced to the next model. The one `finish_reason` that names
   truncation never reached a caller on the primary transport. The chain now names it
   -- but how far a truncation travels is the question `ADVANCE_ON_CROSS_PROVIDER`
   already answers: a sibling rung on the same host hits the same ceiling, while a
   different provider does not spend OpenAI's reasoning budget at all. So it crosses
   providers and is only handed back on the last rung, where the caller fails closed.
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
    LlmResponse,
    ToolCall,
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


def _responses_stop(text: str) -> dict:
    return {
        "status": "completed",
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]}
        ],
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def _consent_call(decision: str, value: str) -> dict:
    """A well-formed classify_contact_consent call, as the Gemini rung would emit it."""
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "call_c1",
                            "type": "function",
                            "function": {
                                "name": "classify_contact_consent",
                                "arguments": json.dumps(
                                    {
                                        "decision": decision,
                                        "evidence": value,
                                        "contact_span": value,
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 10},
    }


class _Script:
    """Replays one body per request and records how many rungs were actually called."""

    def __init__(self, bodies: list[dict]) -> None:
        self._bodies = list(bodies)
        self.requests: list[dict] = []
        self.hosts: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(json.loads(request.content.decode("utf-8")))
        self.hosts.append(request.url.host)
        index = min(len(self.requests) - 1, len(self._bodies) - 1)
        return httpx.Response(200, json=self._bodies[index])


def test_a_truncation_is_not_spent_on_a_same_provider_sibling(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Two rungs on one host: the sibling runs the same prompt into the same ceiling.

    Same doctrine as ADVANCE_ON_CROSS_PROVIDER. Before the fix the chain read the
    stripped shape as "no text and no tools", recorded `primary:empty_reply`, and burned
    the sibling; the one finish_reason that names truncation never reached a caller.
    """
    script = _Script([_responses_truncation(), _responses_stop("sibling answered")])
    http = httpx.Client(transport=httpx.MockTransport(script.handle))
    chain = LlmModelChain(
        [
            LlmClient(api_key="k", model="primary", url=OPENAI_RESPONSES_URL, client=http),
            LlmClient(api_key="k", model="sibling", url=OPENAI_RESPONSES_URL, client=http),
        ]
    )

    with caplog.at_level(logging.WARNING, logger="app.integrations.llm_client"):
        response = chain.complete(messages=[{"role": "user", "content": "מה יש היום"}])

    assert response.finish_reason == "length"
    assert response.truncated() is True
    assert len(script.requests) == 1, "the same-host sibling must never have been called"
    assert chain.last_model == "primary"
    assert chain.errors == ["primary:truncated"], "the reason code must reach the caller"
    assert any(
        "llm chain returned truncation reason=truncated" in record.getMessage()
        for record in caplog.records
    )


def test_a_truncation_still_crosses_to_the_other_provider(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The Gemini rung does not spend OpenAI's reasoning budget, so it is still tried.

    Both real chains (build_site_client, build_agent_client) are OpenAI Responses rungs
    followed by a Gemini rung, so this is the normal shape. Returning the truncation
    here instead of advancing removed the cross-provider rescue outright.
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

    assert response.truncated() is False
    assert response.text == "fallback answered"
    assert script.hosts == ["api.openai.com", "generativelanguage.googleapis.com"]
    assert chain.last_model == "fallback"
    assert chain.errors == ["primary:truncated"], "not empty_reply -- it was a truncation"
    assert any(
        "llm chain advancing past truncation reason=truncated" in record.getMessage()
        for record in caplog.records
    )


def test_a_truncation_on_the_last_rung_still_fails_closed() -> None:
    """Once the cross-provider rescue is exhausted, the truncation reaches the caller.

    This is what keeps the owner agent fail-closed: Gemini is last on both real chains,
    so a truncation there is returned rather than swallowed as `empty_reply`.
    """
    script = _Script([_responses_truncation(), _chat_truncation()])
    http = httpx.Client(transport=httpx.MockTransport(script.handle))
    chain = LlmModelChain(
        [
            LlmClient(api_key="k", model="primary", url=OPENAI_RESPONSES_URL, client=http),
            LlmClient(api_key="k2", model="fallback", url=GEMINI_CHAT_URL, client=http),
        ]
    )

    response = chain.complete(messages=[{"role": "user", "content": "מה יש היום"}])

    assert response.truncated() is True
    assert response.text == ""
    assert response.tool_calls == ()
    assert len(script.requests) == 2
    assert chain.errors == ["primary:truncated", "fallback:truncated"]


def test_a_truncated_primary_still_captures_the_website_lead() -> None:
    """The harm the chain guard has to avoid: a dropped lead on the capture path.

    `_classified_consent` treats `finish_reason == "length"` as unresolved and fails
    closed, so a chain that returns the primary's truncation instead of crossing to
    Gemini turns a captured lead into a dropped one. Server-side extraction still owns
    the value; only the consent verdict is at stake here.
    """
    from app.surfaces import site_v2

    script = _Script([_responses_truncation(), _consent_call("affirmative", "0501234567")])
    http = httpx.Client(transport=httpx.MockTransport(script.handle))
    chain = LlmModelChain(
        [
            LlmClient(api_key="k", model="primary", url=OPENAI_RESPONSES_URL, client=http),
            LlmClient(api_key="k2", model="fallback", url=GEMINI_CHAT_URL, client=http),
        ]
    )

    verdict = site_v2._classified_consent(
        chain,
        text="בטח, 0501234567",
        contact_value="0501234567",
        prior_invitation="אפשר מספר טלפון?",
    )

    assert verdict.decision == "affirmative"
    assert verdict.resolved is True
    assert script.hosts == ["api.openai.com", "generativelanguage.googleapis.com"]


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


def test_a_truncated_chat_refusal_is_still_reported_as_a_refusal() -> None:
    """A refusal is a decided answer about the request, not a fragment of output.

    The strip drops partial prose and partial calls on purpose. Dropping the refusal
    with them turned "this model refused" into the weaker, less honest "truncated" --
    and `run_owner_agent` checks `refused()` before `truncated()`, so the owner-facing
    reason code changed too.
    """
    body = {
        "choices": [
            {
                "finish_reason": "length",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "refusal": "I can't help with that",
                },
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
    }
    script = _Script([body])
    client = LlmClient(
        api_key="k",
        model="gemini-fallback",
        url=GEMINI_CHAT_URL,
        client=httpx.Client(transport=httpx.MockTransport(script.handle)),
    )

    response = client.complete(messages=[{"role": "user", "content": "x"}])

    assert response.truncated() is True
    assert response.refused() is True
    assert response.refusal == "I can't help with that"
    assert response.text == "", "the stripped prose must still be gone"
    assert response.tool_calls == ()


def test_an_untruncated_chat_refusal_reads_exactly_as_it_did_before() -> None:
    """The other side of the hoisted `refusal`: the ordinary stop path is unchanged.

    `refusal` is now read once, above the truncation branch, and used by both returns.
    This pins the non-truncated site so the shared expression cannot drift.
    """
    body = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "refusal": "  I can't help with that  ",
                },
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 7},
    }
    script = _Script([body])
    client = LlmClient(
        api_key="k",
        model="gemini-fallback",
        url=GEMINI_CHAT_URL,
        client=httpx.Client(transport=httpx.MockTransport(script.handle)),
    )

    response = client.complete(messages=[{"role": "user", "content": "x"}])

    assert response.truncated() is False
    assert response.refused() is True
    assert response.refusal == "I can't help with that", "still stripped of whitespace"


def _run_owner_turn(client: object) -> tuple[object, list[str]]:
    """Run one owner turn against `client`, recording every tool dispatch it attempts."""
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
        owner_agent._run_tool_with_timeout = _record  # type: ignore[assignment]
        outcome = run_owner_agent(client=client, ctx=ctx, owner_message="מה יש היום")
    finally:
        owner_agent._run_tool_with_timeout = original  # type: ignore[assignment]
        db.close()
    return outcome, executed


def test_a_truncated_owner_turn_reports_truncated_and_runs_no_tool() -> None:
    """The owner loop must stop at a truncation instead of dispatching half an intent.

    Before the fix the chat transport's partial prose made
    `response.truncated() and not response.text` false, and the loop executed the
    `daily_brief` call built from a cut-off argument string.
    """
    script = _Script([_chat_truncation()])
    client = LlmClient(
        api_key="k",
        model="gemini-fallback",
        url=GEMINI_CHAT_URL,
        client=httpx.Client(transport=httpx.MockTransport(script.handle)),
    )

    outcome, executed = _run_owner_turn(client)

    assert outcome.completion == "truncated"
    assert outcome.completed is False
    assert outcome.text == "", "partial prose must never reach the owner as an answer"
    assert executed == []
    assert outcome.tools_used == ()


class _PartialProseClient:
    """A transport that does NOT strip: partial prose plus a call cut mid-JSON.

    Both shipped transports strip a truncation to nothing, which is exactly why the
    owner-loop guard needs its own test: with the strip in place, dropping
    `and not response.text` from the guard changes no shipped behaviour and every
    other test still passes. This client holds the guard to its stated contract --
    a truncation stops the turn on the finish_reason alone, whatever prose came with
    it -- so a future transport that returns partial output cannot quietly walk into
    tool dispatch.
    """

    model = "partial-prose"

    def enabled(self) -> bool:
        return True

    def complete(self, **kwargs: object) -> LlmResponse:
        return LlmResponse(
            text="רגע, אני מושכת את הסיכום היומי ו",
            tool_calls=(
                ToolCall(
                    call_id="call_p1",
                    name=TRUNCATED_TOOL,
                    # What parse_tool_arguments normalises a cut argument string to.
                    arguments={},
                    raw_arguments=CUT_ARGUMENTS,
                ),
            ),
            finish_reason="length",
            refusal="",
            tokens_in=120,
            tokens_out=180,
            raw_message={"role": "assistant"},
        )


def test_partial_prose_does_not_carry_a_truncated_call_into_dispatch() -> None:
    """finish_reason alone stops the turn; prose alongside it changes nothing."""
    outcome, executed = _run_owner_turn(_PartialProseClient())

    assert outcome.completion == "truncated"
    assert outcome.completed is False
    assert outcome.text == "", "partial prose must never reach the owner as an answer"
    assert executed == [], "a call built from a cut argument string must not run"
    assert outcome.tools_used == ()
