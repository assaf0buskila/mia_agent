from __future__ import annotations

import json

import httpx
import pytest
from app.core.config import Settings
from app.integrations.llm_client import (
    GEMINI_CHAT_URL,
    OPENAI_RESPONSES_URL,
    LlmClient,
    LlmModelChain,
    build_site_client,
    function_tool,
    tool_result_message,
)
from app.integrations.transcribe import (
    FallbackTranscriptionPort,
    GeminiTranscribePort,
    OpenAITranscribePort,
)
from app.workers import telegram_owner


def _tool() -> dict:
    return function_tool(
        name="lookup",
        description="lookup",
        parameters={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    )


def test_site_builder_uses_only_site_ids_and_maps_gemini_reasoning() -> None:
    settings = Settings(
        _env_file=None,
        openai_api_key="openai-test",
        gemini_api_key="gemini-test",
        sales_model="site-primary",
        sales_fallback_model="site-secondary",
        sales_gemini_model="site-gemini",
        owner_agent_model="owner-only",
        sales_reasoning_effort="xhigh",
    )
    chain = build_site_client(settings)
    assert chain.models == ("site-primary", "site-secondary", "site-gemini")
    assert [client._url for client in chain._clients] == [
        OPENAI_RESPONSES_URL,
        OPENAI_RESPONSES_URL,
        GEMINI_CHAT_URL,
    ]
    assert chain._clients[-1]._reasoning_effort == "high"


def test_cross_provider_tool_continuation_keeps_results_and_declared_tools() -> None:
    openai_requests: list[dict] = []
    gemini_requests: list[dict] = []

    def openai_handler(request: httpx.Request) -> httpx.Response:
        openai_requests.append(json.loads(request.content))
        if len(openai_requests) == 1:
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output": [
                        {
                            "type": "reasoning",
                            "id": "reasoning-private",
                            "encrypted_content": "opaque",
                        },
                        {
                            "type": "function_call",
                            "call_id": "call-first",
                            "name": "lookup",
                            "arguments": '{"query":"first"}',
                        },
                    ],
                },
            )
        return httpx.Response(503, json={"error": {"message": "not exposed"}})

    def gemini_handler(request: httpx.Request) -> httpx.Response:
        gemini_requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call-next",
                                    "type": "function",
                                    "function": {
                                        "name": "lookup",
                                        "arguments": '{"query":"next"}',
                                    },
                                }
                            ],
                        },
                    }
                ]
            },
        )

    chain = LlmModelChain(
        [
            LlmClient(
                api_key="openai-test",
                model="owner-primary",
                url=OPENAI_RESPONSES_URL,
                client=httpx.Client(transport=httpx.MockTransport(openai_handler)),
            ),
            LlmClient(
                api_key="gemini-test",
                model="owner-gemini",
                url=GEMINI_CHAT_URL,
                client=httpx.Client(transport=httpx.MockTransport(gemini_handler)),
            ),
        ]
    )
    messages = [{"role": "user", "content": "start"}]
    first = chain.complete(messages=messages, tools=[_tool()])
    messages.extend(
        [first.raw_message, tool_result_message("call-first", {"ok": True, "value": 1})]
    )
    second = chain.complete(messages=messages, tools=[_tool()])

    assert second.tool_calls[0].arguments == {"query": "next"}
    payload = gemini_requests[0]
    assert payload["tools"] == [_tool()]
    assert any(message.get("role") == "tool" for message in payload["messages"])
    assert all("_responses_output" not in message for message in payload["messages"])


@pytest.mark.asyncio
async def test_openai_stt_failure_falls_back_to_native_gemini_with_provenance() -> None:
    openai = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(503, json={"error": {"message": "secret body"}})
        )
    )
    gemini_payloads: list[dict] = []

    def gemini_handler(request: httpx.Request) -> httpx.Response:
        gemini_payloads.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"parts": [{"text": "דברי עם [לא ברור] מחר"}]}}]},
        )

    gemini = httpx.AsyncClient(transport=httpx.MockTransport(gemini_handler))
    port = FallbackTranscriptionPort(
        (
            OpenAITranscribePort(api_key="openai-test", model="gpt-transcribe", client=openai),
            GeminiTranscribePort(
                api_key="gemini-test", model="configured-transcriber", client=gemini
            ),
        )
    )
    try:
        result = await port.transcribe(audio=b"audio", mime_type="audio/ogg")
    finally:
        await openai.aclose()
        await gemini.aclose()

    assert result.text == "דברי עם [לא ברור] מחר"
    assert result.stt_provider == "gemini"
    assert result.stt_model == "configured-transcriber"
    assert result.language == result.confidence == ""
    part = gemini_payloads[0]["contents"][0]["parts"][1]["inline_data"]
    assert part["mime_type"] == "audio/ogg"
    assert part["data"] == "YXVkaW8="


def test_readiness_is_purpose_specific() -> None:
    sales_only = Settings(
        _env_file=None,
        openai_api_key="openai-test",
        sales_model="site-only",
        owner_agent_model="",
    )
    assert sales_only.sales_llm_ready() is True
    assert sales_only.owner_agent_ready() is False


@pytest.mark.asyncio
async def test_photo_caption_informs_vision_and_remains_in_owner_turn(monkeypatch) -> None:
    captured: dict[str, str] = {}

    class Media:
        async def download_photo(self, _file_id: str) -> tuple[bytes, str]:
            return b"pixels", "image/jpeg"

    def describe(payload: bytes, mime: str, *, caption: str = "") -> str:
        captured.update(payload=payload.decode(), mime=mime, caption=caption)
        return "נראה חשבון עם סכום לא ברור"

    monkeypatch.setattr(telegram_owner, "_describe_owner_image", describe)
    result = await telegram_owner._see_telegram_photo(
        item={"text": "זה החשבון של דני?"},
        media=Media(),
        photo_file_id="photo-1",
    )

    assert captured["caption"] == "זה החשבון של דני?"
    assert "נראה חשבון" in result["text"]
    assert "זה החשבון של דני?" in result["text"]
