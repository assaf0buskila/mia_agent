import inspect
import json
import time

import httpx
import pytest
from app.api.inbound import process_inbound_texts
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.owner_tasks import OwnerTaskType, ack_for_owner_task, classify_owner_task
from app.domain.tools import AdapterHttpError
from app.integrations.base import RecordingMessagePort
from app.integrations.meta_ads import DisabledMetaAdsPort
from app.integrations.research import (
    DisabledResearchPort,
    FakeResearchPort,
    FirecrawlSearchPort,
    ResearchPort,
    ResearchSnippet,
    build_research_port,
    enrich_research_ack,
    format_sources_block,
    sanitize_snippets,
)
from app.integrations.sheets import FakeSheetsPort

from tests.unit.sales_copy import assert_discovery_reply

OWNER_RESEARCH_PHONE = "972509990007"
PROSPECT_AUDIO_PHONE = "972509990008"

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


def test_enrich_with_fake_appends_title_host_and_keeps_not_executed() -> None:
    decision = classify_owner_task("Do competitor research on Acme")
    assert decision.task_type == OwnerTaskType.RESEARCH
    ack = ack_for_owner_task(decision)
    enriched, _outcome = enrich_research_ack(
        ack,
        FakeResearchPort(SAMPLE_SNIPPETS),
        query="Do competitor research on Acme",
        kill_switch=False,
    )
    assert "לא ביצעתי" in enriched
    assert "מקורות ציבוריים (לא בוצע):" in enriched
    assert "Acme Corp Overview — www.acme.com" in enriched
    assert "Acme Competitor Analysis — example.com" in enriched


def test_disabled_enrich_unchanged() -> None:
    decision = classify_owner_task("Do competitor research on Acme")
    ack = ack_for_owner_task(decision)
    enriched, _outcome = enrich_research_ack(
        ack,
        DisabledResearchPort(),
        query="Do competitor research on Acme",
        kill_switch=False,
    )
    assert enriched == ack
    assert "מקורות ציבוריים" not in enriched


def test_kill_switch_skips_port_call() -> None:
    class RaisingResearchPort:
        def search(self, query: str) -> list[ResearchSnippet]:
            del query
            raise RuntimeError("must not call port when kill switch is on")

    decision = classify_owner_task("Do competitor research on Acme")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_research_ack(
        ack,
        RaisingResearchPort(),
        query="Do competitor research on Acme",
        kill_switch=True,
    )
    assert enriched == ack
    assert outcome.status == "denied"


def test_enrich_research_ack_measures_port_latency() -> None:
    class SlowResearchPort(FakeResearchPort):
        def search(self, query: str) -> list[ResearchSnippet]:
            time.sleep(0.02)
            return super().search(query)

    decision = classify_owner_task("Do competitor research on Acme")
    ack = ack_for_owner_task(decision)
    port = SlowResearchPort(
        [
            ResearchSnippet(
                title="Latency Proof",
                url="https://latency.example.com/page",
                excerpt="proof snippet",
            )
        ]
    )
    enriched, outcome = enrich_research_ack(
        ack,
        port,
        query="Do competitor research on Acme",
        kill_switch=False,
    )
    assert "Latency Proof" in enriched
    assert outcome.latency_ms >= 15


@pytest.mark.asyncio
async def test_owner_research_inbound_persists_measured_latency() -> None:
    class SlowResearchPort(FakeResearchPort):
        def search(self, query: str) -> list[ResearchSnippet]:
            time.sleep(0.02)
            return super().search(query)

    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "wamid.tool.lat.research.1",
                    "from": OWNER_RESEARCH_LATENCY_PHONE,
                    "text": "Do competitor research on Acme",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_RESEARCH_LATENCY_PHONE},
            sheets=sheets,
            meta_ads=DisabledMetaAdsPort(),
            research=SlowResearchPort(SAMPLE_SNIPPETS),
        )
        db.commit()
        tool_run = store.get_tool_run(
            "wamid.tool.lat.research.1:tool:research_search"
        )
        assert tool_run is not None
        assert tool_run.latency_ms >= 15
    finally:
        db.close()


def test_non_https_snippet_dropped() -> None:
    mixed = [
        ResearchSnippet(title="Bad", url="http://insecure.example.com", excerpt="x"),
        ResearchSnippet(title="Good", url="https://secure.example.com/page", excerpt="y"),
    ]
    cleaned = sanitize_snippets(mixed)
    assert len(cleaned) == 1
    assert cleaned[0].title == "Good"
    assert cleaned[0].url == "https://secure.example.com/page"
    assert sanitize_snippets(
        [ResearchSnippet(title="Empty host", url="https://", excerpt="")]
    ) == []


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


@pytest.mark.asyncio
async def test_owner_research_fake_titles_in_sent_text() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        sheets = FakeSheetsPort()
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.owner.research.1",
                    "from": OWNER_RESEARCH_PHONE,
                    "text": "Do competitor research on Acme",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_RESEARCH_PHONE},
            sheets=sheets,
            meta_ads=DisabledMetaAdsPort(),
            research=FakeResearchPort(SAMPLE_SNIPPETS),
        )
        db.commit()
        task = store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.owner.research.1"
        )
        assert task is not None
        assert task.task_type == "research"
        assert task.status == "logged"
        assert sheets.rows == {}
        sent = port.sent[0].text
        assert "Acme Corp Overview" in sent
        assert "Acme Competitor Analysis" in sent
        assert "לא ביצעתי" in sent
        assert "how the business works" not in sent
        assert "יום רגיל בעסק" not in sent
        tool_row = store.get_canonical_event(
            provider="whatsapp",
            provider_event_id="evt.owner.research.1:tool:research_search",
        )
        assert tool_row is not None
        payload = json.loads(tool_row.payload_json)
        assert payload["status"] == "ok"
        assert payload["result_count"] == 2
        serialized = json.dumps(payload).lower()
        assert "http" not in serialized
        assert "acme" not in serialized
        assert "url" not in serialized
    finally:
        db.close()


@pytest.mark.asyncio
async def test_prospect_audio_does_not_call_research() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()

        class ExplodingResearchPort:
            def search(self, query: str) -> list[ResearchSnippet]:
                del query
                raise RuntimeError("research must not run on prospect path")

        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "evt.prospect.research.nopath.1",
                    "from": PROSPECT_AUDIO_PHONE,
                    "text": "hi there",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            research=ExplodingResearchPort(),
        )
        db.commit()
        assert store.get_owner_task(
            provider="whatsapp", provider_event_id="evt.prospect.research.nopath.1"
        ) is None
        assert len(port.sent) == 1
        assert_discovery_reply(port.sent[0].text)
        assert "מקורות ציבוריים" not in port.sent[0].text
    finally:
        db.close()


def test_protocol_has_no_write_or_crawl_methods() -> None:
    forbidden = ("create", "update", "delete", "crawl", "scrape", "browse")
    protocol_methods = {
        name
        for name, _ in inspect.getmembers(ResearchPort, predicate=inspect.isfunction)
    }
    for name in protocol_methods:
        lowered = name.lower()
        assert not any(token in lowered for token in forbidden)

    for impl in (
        DisabledResearchPort(),
        FakeResearchPort(SAMPLE_SNIPPETS),
        FirecrawlSearchPort(api_key="fc-test-key"),
    ):
        for name in dir(impl):
            if name.startswith("_"):
                continue
            lowered = name.lower()
            assert not any(token in lowered for token in forbidden)


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
    settings = Settings(firecrawl_api_key="   ")
    port = build_research_port(settings)
    assert isinstance(port, DisabledResearchPort)


def test_enrich_research_ack_http_401_unauthorized_ack_unchanged() -> None:
    transport = httpx.MockTransport(lambda _request: httpx.Response(401))
    client = httpx.Client(transport=transport)
    port = FirecrawlSearchPort(api_key="fc-test", client=client)
    decision = classify_owner_task("Do competitor research on Acme")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_research_ack(
        ack,
        port,
        query="Do competitor research on Acme",
        kill_switch=False,
    )
    assert enriched == ack
    assert outcome.status == "unauthorized"
    assert outcome.latency_ms >= 0
    assert "מקורות ציבוריים" not in enriched


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
