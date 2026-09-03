"""An OpenAI-side outage must actually reach Gemini.

`LlmModelChain` deliberately refuses to spend a same-provider fallback rung on a
transient 5xx/429 — advancing from one OpenAI model to another during an OpenAI outage
just fails twice. That reasoning does not extend to the Gemini rung, which is a
different provider and exists precisely as the outage safety net. These pin both halves.
"""

from __future__ import annotations

import httpx
import pytest
from app.integrations.llm_client import (
    GEMINI_CHAT_URL,
    OPENAI_CHAT_URL,
    LlmClient,
    LlmError,
    LlmModelChain,
)

OUTAGE_STATUSES = (500, 502, 503, 504, 429, 401)


def _ok(text: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": text, "role": "assistant"}}]},
        )

    return handler


def _status(code: int):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(code, json={"error": {"message": "boom"}})

    return handler


def _client(model: str, handler, *, url: str = OPENAI_CHAT_URL) -> LlmClient:
    return LlmClient(
        api_key="k",
        model=model,
        url=url,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.parametrize("code", OUTAGE_STATUSES)
def test_openai_outage_falls_over_to_gemini(code: int) -> None:
    chain = LlmModelChain(
        [
            _client("openai-primary", _status(code)),
            _client("gemini", _ok("שלום"), url=GEMINI_CHAT_URL),
        ]
    )
    assert chain.complete(messages=[{"role": "user", "content": "hi"}]).text == "שלום"
    assert chain.last_model == "gemini"


@pytest.mark.parametrize("code", OUTAGE_STATUSES)
def test_outage_does_not_burn_a_same_provider_rung(code: int) -> None:
    """Unchanged behaviour: another OpenAI model shares the same outage."""
    chain = LlmModelChain(
        [
            _client("openai-primary", _status(code)),
            _client("openai-fallback", _ok("x")),
        ]
    )
    with pytest.raises(LlmError):
        chain.complete(messages=[{"role": "user", "content": "hi"}])
    assert chain.last_model == "openai-primary"


def test_full_chain_skips_sibling_then_crosses_to_gemini() -> None:
    """Primary 503 must not stop at the sibling; it must reach the other provider."""
    chain = LlmModelChain(
        [
            _client("openai-primary", _status(503)),
            _client("openai-fallback", _status(503)),
            _client("gemini", _ok("נחתנו"), url=GEMINI_CHAT_URL),
        ]
    )
    assert chain.complete(messages=[{"role": "user", "content": "hi"}]).text == "נחתנו"
    assert chain.last_model == "gemini"


def test_access_statuses_still_advance_within_one_provider() -> None:
    """403/404/410 mean this model never works for this key. Unchanged."""
    for code in (403, 404, 410):
        chain = LlmModelChain(
            [_client("blocked", _status(code)), _client("good", _ok("hi"))]
        )
        assert chain.complete(messages=[{"role": "user", "content": "x"}]).text == "hi"
        assert chain.last_model == "good"


def test_provider_is_derived_from_the_endpoint_host() -> None:
    assert _client("a", _ok("x")).provider == "api.openai.com"
    assert (
        _client("b", _ok("x"), url=GEMINI_CHAT_URL).provider
        == "generativelanguage.googleapis.com"
    )
