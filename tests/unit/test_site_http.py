from types import SimpleNamespace

import httpx
import pytest
from app.integrations.llm_client import build_site_client


def _settings(*, api_key: str, timeout: float = 7.5) -> SimpleNamespace:
    return SimpleNamespace(
        llm_request_timeout_seconds=timeout,
        openai_api_key=api_key,
        sales_model="primary",
        sales_fallback_model="",
        sales_reasoning_effort="low",
        gemini_api_key="",
        sales_gemini_model="",
    )


def test_site_builders_share_only_the_reusable_http_pool(monkeypatch) -> None:
    shared = httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
    monkeypatch.setattr("app.integrations.llm_client.get_site_http_client", lambda: shared)
    first = build_site_client(_settings(api_key="first-secret"))
    second = build_site_client(_settings(api_key="second-secret"))

    assert first is not second
    assert first._clients[0] is not second._clients[0]
    assert first._clients[0]._client is shared
    assert second._clients[0]._client is shared
    assert "Authorization" not in shared.headers
    shared.close()


def test_site_shared_pool_keeps_auth_and_timeout_request_specific() -> None:
    observed: list[tuple[str, dict[str, float]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        observed.append((request.headers["Authorization"], request.extensions["timeout"]))
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "ok"}]}
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
            },
        )

    shared = httpx.Client(transport=httpx.MockTransport(handler))
    first = build_site_client(_settings(api_key="first-secret"), http_client=shared)
    second = build_site_client(_settings(api_key="second-secret"), http_client=shared)
    first.complete(messages=[{"role": "user", "content": "one"}], timeout=1.25)
    second.complete(messages=[{"role": "user", "content": "two"}], timeout=2.5)

    assert [item[0] for item in observed] == ["Bearer first-secret", "Bearer second-secret"]
    assert observed[0][1]["connect"] == pytest.approx(1.25, abs=0.01)
    assert observed[1][1]["connect"] == pytest.approx(2.5, abs=0.01)
    shared.close()
