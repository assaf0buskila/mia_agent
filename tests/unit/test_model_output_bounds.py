"""Production model calls carry a completion bound.

`LlmClient` has supported `max_completion_tokens` all along and no caller ever passed
one, so a runaway generation was billed in full and the first cost signal was the
invoice.

The parameter name is not cosmetic. OpenAI removed `max_tokens` from Chat Completions
in favour of `max_completion_tokens`; Gemini's OpenAI-compat endpoint still speaks
`max_tokens`. Sending the wrong one is a 400, and a 400 on the website path drops the
visitor to a canned reply — so the key is chosen per endpoint, and that is pinned here.
"""

from __future__ import annotations

import json

import httpx
from app.core.config import Settings
from app.domain.sales import NextAction
from app.integrations.sales_reply import OpenAISalesReplyPort, build_sales_reply_port


def _capture(status: int = 200):
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"url": str(request.url), **body})
        return httpx.Response(
            status, json={"choices": [{"message": {"content": "בסדר, ספרו עוד."}}]}
        )

    return seen, handler


def _compose(port: OpenAISalesReplyPort) -> None:
    port.compose(
        action=NextAction.UNDERSTAND_WORKFLOW,
        canned="תודה שפניתם.",
        latest_message="צריך אתר",
        channel="website",
        kill_switch=False,
    )


def test_openai_gets_max_completion_tokens() -> None:
    seen, handler = _capture()
    port = OpenAISalesReplyPort(
        api_key="sk-test",
        model="test-sales-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_completion_tokens=500,
    )
    _compose(port)
    assert seen
    assert "api.openai.com" in seen[0]["url"]
    assert seen[0]["max_completion_tokens"] == 500
    assert "max_tokens" not in seen[0]


def test_gemini_compat_gets_max_tokens_instead() -> None:
    seen, handler = _capture()
    port = OpenAISalesReplyPort(
        api_key="",
        model="",
        gemini_api_key="gem-test",
        gemini_model="gemini-3.6-flash",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_completion_tokens=500,
    )
    _compose(port)
    assert seen
    assert "generativelanguage.googleapis.com" in seen[0]["url"]
    assert seen[0]["max_tokens"] == 500
    assert "max_completion_tokens" not in seen[0]


def test_zero_sends_no_bound_at_all() -> None:
    """0 is the escape hatch back to the old behaviour, not a bound of zero."""
    seen, handler = _capture()
    port = OpenAISalesReplyPort(
        api_key="sk-test",
        model="test-sales-model",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        max_completion_tokens=0,
    )
    _compose(port)
    assert seen
    assert "max_completion_tokens" not in seen[0]
    assert "max_tokens" not in seen[0]


def test_the_built_website_port_carries_the_configured_bound() -> None:
    settings = Settings(
        _env_file=None,
        openai_api_key="sk-test",
        sales_model="test-sales-model",
    )
    port = build_sales_reply_port(settings)
    assert isinstance(port, OpenAISalesReplyPort)
    # Wired from config, and short: the website answers in a sentence or two.
    assert port._max_completion_tokens == settings.max_completion_tokens_site
    assert 0 < settings.max_completion_tokens_site < settings.max_completion_tokens_owner
