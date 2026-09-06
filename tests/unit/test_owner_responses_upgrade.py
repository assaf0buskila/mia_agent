from __future__ import annotations

import json
from time import monotonic

import httpx
import pytest
from app.domain.tools import AdapterHttpError
from app.graph import owner_agent
from app.graph.owner_agent import TOOL_DEADLINE_REPLY, _run_tool_with_timeout, build_messages
from app.integrations.base import OutboundMessage
from app.integrations.llm_client import (
    GEMINI_CHAT_URL,
    OPENAI_RESPONSES_URL,
    LlmClient,
    LlmError,
    LlmModelChain,
    function_tool,
    tool_result_message,
)
from app.integrations.telegram import TelegramPort, TelegramSendError


def test_owner_responses_replays_encrypted_reasoning_and_keeps_tools_serial() -> None:
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if len(requests) == 1:
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "reasoning",
                            "id": "rs_1",
                            "encrypted_content": "opaque",
                            "summary": [],
                        },
                        {
                            "type": "function_call",
                            "id": "fc_1",
                            "call_id": "call_1",
                            "name": "lookup",
                            "arguments": '{"query":"Daniel"}',
                            "status": "completed",
                        },
                    ],
                    "usage": {"input_tokens": 10, "output_tokens": 7},
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": "מצאתי."}],
                    }
                ],
                "usage": {"input_tokens": 20, "output_tokens": 3},
            },
        )

    client = LlmClient(
        api_key="not-a-real-key",
        model="configured-owner-model",
        url=OPENAI_RESPONSES_URL,
        reasoning_effort="medium",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    tools = [
        function_tool(
            name="lookup",
            description="lookup",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
                "additionalProperties": False,
            },
        )
    ]
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "find"}]
    first = client.complete(messages=messages, tools=tools, parallel_tool_calls=False)
    messages.extend([first.raw_message, tool_result_message("call_1", {"ok": True})])
    second = client.complete(messages=messages, tools=tools, parallel_tool_calls=False)

    assert second.text == "מצאתי."
    assert requests[0]["reasoning"] == {"effort": "medium"}
    assert requests[0]["store"] is False
    assert requests[0]["include"] == ["reasoning.encrypted_content"]
    assert requests[0]["parallel_tool_calls"] is False
    assert requests[0]["tools"][0]["name"] == "lookup"
    replay = requests[1]["input"]
    assert any(
        item.get("type") == "reasoning" and item.get("encrypted_content") == "opaque"
        for item in replay
    )
    assert any(
        item.get("type") == "function_call_output" and item.get("call_id") == "call_1"
        for item in replay
    )


def test_audio_input_requires_clarification_instead_of_guessing() -> None:
    system = build_messages(
        owner_message="תקבעי עם דניאל מחר בשתיים",
        history=(),
        context=None,
        input_source="audio",
    )[0]["content"]
    assert "AUDIO INPUT" in system
    assert "names, numbers and dates" in system
    assert "Never guess" in system


def test_expired_deadline_does_not_start_a_tool(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        owner_agent,
        "execute_tool",
        lambda *_args, **_kwargs: calls.append("started"),
    )
    result = _run_tool_with_timeout(
        "crm_search",
        {},
        object(),
        deadline_at=monotonic() - 1,  # type: ignore[arg-type]
    )
    assert calls == []
    assert result.ok is False
    assert result.text == TOOL_DEADLINE_REPLY


def test_responses_tool_history_converts_for_cross_provider_fallback() -> None:
    openai_calls = 0
    gemini_payloads: list[dict] = []

    def openai_handler(_request: httpx.Request) -> httpx.Response:
        nonlocal openai_calls
        openai_calls += 1
        if openai_calls == 1:
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "reasoning",
                            "id": "rs_cross",
                            "encrypted_content": "opaque-cross",
                            "summary": [],
                        },
                        {
                            "type": "function_call",
                            "id": "fc_cross",
                            "call_id": "call_cross",
                            "name": "lookup",
                            "arguments": "{}",
                        },
                    ],
                },
            )
        return httpx.Response(503, json={"error": {"message": "outage"}})

    def gemini_handler(request: httpx.Request) -> httpx.Response:
        gemini_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "done"}}]},
        )

    chain = LlmModelChain(
        [
            LlmClient(
                api_key="k",
                model="owner",
                url=OPENAI_RESPONSES_URL,
                reasoning_effort="medium",
                client=httpx.Client(transport=httpx.MockTransport(openai_handler)),
            ),
            LlmClient(
                api_key="k",
                model="gemini",
                url=GEMINI_CHAT_URL,
                client=httpx.Client(transport=httpx.MockTransport(gemini_handler)),
            ),
        ]
    )
    messages = [{"role": "user", "content": "look"}]
    first = chain.complete(messages=messages)
    messages.extend([first.raw_message, tool_result_message("call_cross", {"ok": True})])
    assert chain.complete(messages=messages).text == "done"
    sent = gemini_payloads[0]["messages"]
    assistant = next(message for message in sent if message.get("role") == "assistant")
    assert assistant["tool_calls"][0]["id"] == "call_cross"
    assert "_responses_output" not in assistant


@pytest.mark.parametrize("status,reason", [("failed", ""), ("incomplete", "content_filter")])
def test_responses_noncompleted_output_is_never_exposed(status: str, reason: str) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        body = {
            "status": status,
            "output": [
                {"type": "function_call", "call_id": "unsafe", "name": "lookup", "arguments": "{}"},
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "partial"}],
                },
            ],
        }
        if reason:
            body["incomplete_details"] = {"reason": reason}
        return httpx.Response(200, json=body)

    client = LlmClient(
        api_key="k",
        model="owner",
        url=OPENAI_RESPONSES_URL,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    with pytest.raises(LlmError, match="not completed"):
        client.complete(messages=[{"role": "user", "content": "look"}])


def test_responses_max_output_truncation_never_exposes_function_call() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "status": "incomplete",
                "incomplete_details": {"reason": "max_output_tokens"},
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "partial_call",
                        "name": "lookup",
                        "arguments": '{"query":"Daniel"}',
                    }
                ],
                "usage": {"input_tokens": 4, "output_tokens": 9},
            },
        )

    client = LlmClient(
        api_key="k",
        model="owner",
        url=OPENAI_RESPONSES_URL,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    response = client.complete(messages=[{"role": "user", "content": "look"}])
    assert response.text == ""
    assert response.tool_calls == ()
    assert response.finish_reason == "length"
    assert response.raw_message == {"_responses_output": []}


def _outbound() -> OutboundMessage:
    return OutboundMessage(
        conversation_id="42", text="hello", channel="telegram", idempotency_key="evt:reply"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response,expected_status",
    [
        (httpx.Response(200, text="not-json"), None),
        (httpx.Response(200, json={"ok": False, "description": "rejected"}), 200),
        (httpx.Response(200, json={"ok": True}), None),
    ],
)
async def test_telegram_send_requires_an_explicit_message_receipt(
    response: httpx.Response, expected_status: int | None
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return response

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        port = TelegramPort(bot_token="token", client=http)
        with pytest.raises(TelegramSendError) as caught:
            await port.send(_outbound())
    cause = caught.value.__cause__
    assert isinstance(cause, AdapterHttpError)
    assert cause.status_code == expected_status


@pytest.mark.asyncio
async def test_telegram_send_accepts_message_id_receipt() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 99}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        await TelegramPort(bot_token="token", client=http).send(_outbound())
