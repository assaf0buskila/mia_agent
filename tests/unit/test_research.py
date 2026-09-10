import inspect
import json

import httpx
import pytest
from app.core.config import Settings
from app.domain.tools import AdapterHttpError
from app.integrations.research import (
    ApifySearchPort,
    DisabledResearchPort,
    FakeResearchPort,
    FirecrawlSearchPort,
    ResearchPort,
    ResearchSnippet,
    build_research_port,
    format_sources_block,
    sanitize_snippets,
)

OWNER_RESEARCH_PHONE = "972509990007"

OWNER_RESEARCH_LATENCY_PHONE = "972509998401"

SAMPLE_SNIPPETS = [
    ResearchSnippet(
        title="Acme Corp Overview",
        url="https://www.acme.com/about",
        excerpt="Acme is a leading provider of widgets.",
    ),
    ResearchSnippet(
        title="Acme Competitor Analysis",
        url="https://example.com/acme-review",
        excerpt="Independent review of Acme market position.",
    ),
]


def test_fake_returns_snippets_disabled_returns_empty() -> None:
    fake = FakeResearchPort(SAMPLE_SNIPPETS)
    disabled = DisabledResearchPort()
    assert len(fake.search("acme")) == 2
    assert disabled.search("acme") == []




def test_non_https_snippet_dropped() -> None:
    mixed = [
        ResearchSnippet(title="Bad", url="http://insecure.example.com", excerpt="x"),
        ResearchSnippet(title="Good", url="https://secure.example.com/page", excerpt="y"),
    ]
    cleaned = sanitize_snippets(mixed)
    assert len(cleaned) == 1
    assert cleaned[0].title == "Good"
    assert cleaned[0].url == "https://secure.example.com/page"
    assert (
        sanitize_snippets([ResearchSnippet(title="Empty host", url="https://", excerpt="")]) == []
    )


def test_sanitize_drops_localhost_and_ip_literals() -> None:
    blocked = [
        ResearchSnippet(title="Local", url="https://localhost/page", excerpt=""),
        ResearchSnippet(title="Loopback", url="https://127.0.0.1/page", excerpt=""),
        ResearchSnippet(title="V6", url="https://[::1]/page", excerpt=""),
    ]
    assert sanitize_snippets(blocked) == []
    kept = sanitize_snippets(
        [ResearchSnippet(title="Ok", url="https://ok.example.com/", excerpt="")]
    )
    assert len(kept) == 1


def test_sanitize_strips_title_newlines_before_ack() -> None:
    snippet = ResearchSnippet(
        title="Acme\nInjected owner line",
        url="https://ok.example.com/",
        excerpt="",
    )
    cleaned = sanitize_snippets([snippet])
    assert cleaned[0].title == "Acme Injected owner line"
    block = format_sources_block(cleaned)
    assert block.count("\n") == 1
    assert "Injected owner line" in block
    assert "Acme\nInjected" not in block




def test_protocol_has_no_write_or_crawl_methods() -> None:
    forbidden = ("create", "update", "delete", "crawl", "scrape", "browse")
    protocol_methods = {
        name for name, _ in inspect.getmembers(ResearchPort, predicate=inspect.isfunction)
    }
    for name in protocol_methods:
        lowered = name.lower()
        assert not any(token in lowered for token in forbidden)

    for impl in (
        DisabledResearchPort(),
        FakeResearchPort(SAMPLE_SNIPPETS),
        FirecrawlSearchPort(api_key="fc-test-key"),
        ApifySearchPort(token="apify-test-token"),
    ):
        for name in dir(impl):
            if name.startswith("_"):
                continue
            lowered = name.lower()
            assert not any(token in lowered for token in forbidden)


def test_research_search_tool_hides_actor_catalog() -> None:
    from app.tools.registries.owner_tools import get_tool

    spec = get_tool("research_search")
    assert spec is not None
    lowered = spec.description.lower()
    assert "apify" not in lowered
    assert "actor" not in lowered
    assert "catalog" not in lowered
    assert "playwright" not in lowered
    assert "firecrawl" not in lowered


def test_build_research_port_firecrawl_when_key_set() -> None:
    settings = Settings(firecrawl_api_key="fc-live-key")
    port = build_research_port(settings)
    assert isinstance(port, FirecrawlSearchPort)
    assert not isinstance(port, DisabledResearchPort)


def test_build_research_port_disabled_when_key_empty() -> None:
    settings = Settings(firecrawl_api_key="")
    port = build_research_port(settings)
    assert isinstance(port, DisabledResearchPort)


def test_build_research_port_disabled_when_key_whitespace() -> None:
    settings = Settings(firecrawl_api_key="   ", apify_token="   ")
    port = build_research_port(settings)
    assert isinstance(port, DisabledResearchPort)


def test_build_research_port_apify_when_firecrawl_empty() -> None:
    settings = Settings(firecrawl_api_key="", apify_token="apify-live-token")
    port = build_research_port(settings)
    assert isinstance(port, ApifySearchPort)


def test_build_research_port_firecrawl_wins_when_both_set() -> None:
    settings = Settings(firecrawl_api_key="fc-live-key", apify_token="apify-live-token")
    port = build_research_port(settings)
    assert isinstance(port, FirecrawlSearchPort)
    assert not isinstance(port, ApifySearchPort)


def test_firecrawl_port_http_500_raises_adapter_error() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    client = httpx.Client(transport=transport)
    port = FirecrawlSearchPort(api_key="fc-test", client=client)
    with pytest.raises(AdapterHttpError) as exc_info:
        port.search("acme competitor research")
    assert exc_info.value.status_code == 500


class _RaisingHttpClient:
    def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        raise httpx.HTTPError("network error")


def test_firecrawl_port_network_error_raises_adapter_error() -> None:
    port = FirecrawlSearchPort(
        api_key="fc-test",
        client=_RaisingHttpClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.search("acme competitor research")
    assert exc_info.value.status_code is None


def test_firecrawl_port_maps_success_web_items() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "web": [
                        {
                            "url": "https://www.example.com/",
                            "title": "Example",
                            "description": "Plain snippet",
                            "position": 1,
                        },
                        {
                            "url": "https://docs.example.com/guide",
                            "title": "Guide",
                            "description": "How it works",
                            "position": 2,
                        },
                    ]
                },
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = FirecrawlSearchPort(api_key="fc-test", client=client)
    snippets = port.search("example query")
    assert len(snippets) == 2
    assert snippets[0].title == "Example"
    assert snippets[0].url == "https://www.example.com/"
    assert snippets[0].excerpt == "Plain snippet"
    assert snippets[1].title == "Guide"
    assert snippets[1].excerpt == "How it works"


def test_firecrawl_port_request_body_shape() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "data": {"web": []}})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = FirecrawlSearchPort(api_key="fc-test", client=client)
    port.search("  competitor research on Acme  ")
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["query"] == "competitor research on Acme"
    assert body["limit"] == 2
    assert body["sources"] == ["web"]
    assert body["highlights"] is False
    assert "scrapeOptions" not in body


def test_firecrawl_port_malformed_web_items_returns_empty() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={"success": True, "data": {"web": ["not-a-dict"]}},
        )
    )
    client = httpx.Client(transport=transport)
    port = FirecrawlSearchPort(api_key="fc-test", client=client)
    assert port.search("acme") == []


def test_firecrawl_port_skips_bad_items_keeps_https() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json={
                "success": True,
                "data": {
                    "web": [
                        "not-a-dict",
                        {"title": "No URL"},
                        {
                            "url": "https://www.example.com/",
                            "title": "Kept",
                            "description": "ok",
                        },
                    ]
                },
            },
        )
    )
    client = httpx.Client(transport=transport)
    port = FirecrawlSearchPort(api_key="fc-test", client=client)
    snippets = port.search("acme")
    assert len(snippets) == 1
    assert snippets[0].title == "Kept"
    assert snippets[0].url == "https://www.example.com/"


def test_firecrawl_port_truncates_query_to_max_len() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True, "data": {"web": []}})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = FirecrawlSearchPort(api_key="fc-test", client=client)
    port.search("q" * 250)
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["query"] == "q" * 200


def test_firecrawl_port_empty_query_skips_http() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"success": True, "data": {"web": []}})

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = FirecrawlSearchPort(api_key="fc-test", client=client)
    assert port.search("   ") == []
    assert called is False


_APIFY_SAMPLE_PAGE = [
    {
        "organicResults": [
            {
                "title": "Example",
                "url": "https://www.example.com/",
                "description": "Plain snippet",
            },
            {
                "title": "Guide",
                "url": "https://docs.example.com/guide",
                "description": "How it works",
            },
            {
                "title": "Third",
                "url": "https://third.example.com/",
                "description": "Should be dropped",
            },
        ]
    }
]


def test_apify_port_maps_organic_results_caps_two() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(200, json=_APIFY_SAMPLE_PAGE))
    client = httpx.Client(transport=transport)
    port = ApifySearchPort(token="apify-test", client=client)
    snippets = port.search("example query")
    assert len(snippets) == 2
    assert snippets[0].title == "Example"
    assert snippets[0].url == "https://www.example.com/"
    assert snippets[0].excerpt == "Plain snippet"
    assert snippets[1].title == "Guide"
    assert snippets[1].excerpt == "How it works"


def test_apify_port_request_shape_is_adapter_owned() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ApifySearchPort(token="apify-test", client=client)
    port.search("  competitor research on Acme  ")
    url = captured["url"]
    assert isinstance(url, str)
    assert "apify~google-search-scraper" in url
    assert "run-sync-get-dataset-items" in url
    assert "timeout=60" in url
    assert "maxTotalChargeUsd=0.02" in url
    assert "format=json" in url
    assert captured["authorization"] == "Bearer apify-test"
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["queries"] == "competitor research on Acme"
    assert body["maxPagesPerQuery"] == 1
    assert body["saveHtml"] is False
    assert body["saveHtmlToKeyValueStore"] is False
    assert body["focusOnPaidAds"] is False
    assert body["maximumLeadsEnrichmentRecords"] == 0
    assert "memory" not in body
    assert "scrapeOrganicResults" not in body


def test_apify_port_http_408_raises_without_retry() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(408)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ApifySearchPort(token="apify-test", client=client)
    with pytest.raises(AdapterHttpError) as exc_info:
        port.search("acme")
    assert exc_info.value.status_code == 408
    assert calls["n"] == 1


def test_apify_port_http_500_raises_adapter_error() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(500))
    client = httpx.Client(transport=transport)
    port = ApifySearchPort(token="apify-test", client=client)
    with pytest.raises(AdapterHttpError) as exc_info:
        port.search("acme")
    assert exc_info.value.status_code == 500


def test_apify_port_network_error_raises_adapter_error() -> None:
    port = ApifySearchPort(
        token="apify-test",
        client=_RaisingHttpClient(),  # type: ignore[arg-type]
    )
    with pytest.raises(AdapterHttpError) as exc_info:
        port.search("acme")
    assert exc_info.value.status_code is None


def test_apify_port_malformed_page_returns_empty() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json=[{"organicResults": "not-a-list"}])
    )
    client = httpx.Client(transport=transport)
    port = ApifySearchPort(token="apify-test", client=client)
    assert port.search("acme") == []


def test_apify_port_skips_bad_items_keeps_https() -> None:
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            json=[
                {
                    "organicResults": [
                        "not-a-dict",
                        {"title": "No URL"},
                        {
                            "url": "https://www.example.com/",
                            "title": "Kept",
                            "description": "ok",
                        },
                    ]
                }
            ],
        )
    )
    client = httpx.Client(transport=transport)
    port = ApifySearchPort(token="apify-test", client=client)
    snippets = port.search("acme")
    assert len(snippets) == 1
    assert snippets[0].title == "Kept"


def test_apify_port_truncates_query_to_max_len() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["json"] = json.loads(request.content)
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ApifySearchPort(token="apify-test", client=client)
    port.search("q" * 250)
    body = captured["json"]
    assert isinstance(body, dict)
    assert body["queries"] == "q" * 200


def test_apify_port_empty_query_skips_http() -> None:
    called = False

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)
    port = ApifySearchPort(token="apify-test", client=client)
    assert port.search("   ") == []
    assert called is False
