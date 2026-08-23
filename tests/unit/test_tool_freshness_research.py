import json

import pytest
from app.api.inbound import process_inbound_texts
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.owner_tasks import ack_for_owner_task, classify_owner_task
from app.domain.tools import AdapterHttpError
from app.integrations.base import RecordingMessagePort
from app.integrations.meta_ads import DisabledMetaAdsPort
from app.integrations.research import (
    DisabledResearchPort,
    FakeResearchPort,
    ResearchSnippet,
    enrich_research_ack,
)
from app.integrations.sheets import FakeSheetsPort

OWNER_FRESH_RESEARCH_INBOUND_PHONE = "972509998632"

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


def test_enrich_research_ack_fake_freshness_cached() -> None:
    decision = classify_owner_task("Do competitor research on Acme")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_research_ack(
        ack,
        FakeResearchPort(SAMPLE_SNIPPETS),
        query="Do competitor research on Acme",
        kill_switch=False,
    )
    assert outcome.freshness == "cached"
    assert outcome.status == "ok"
    assert outcome.result_count == 2
    assert "מקורות ציבוריים" in enriched
    dumped = json.dumps(outcome.model_dump()).lower()
    assert "http" not in dumped
    assert "acme.com" not in dumped
    assert "example.com" not in dumped
    assert "leading provider of widgets" not in dumped
    assert "independent review" not in dumped


def test_enrich_research_ack_disabled_freshness_unverified() -> None:
    decision = classify_owner_task("Do competitor research on Acme")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_research_ack(
        ack,
        DisabledResearchPort(),
        query="Do competitor research on Acme",
        kill_switch=False,
    )
    assert enriched == ack
    assert outcome.freshness == "unverified"
    assert outcome.status == "empty"


def test_enrich_research_ack_kill_switch_freshness_empty() -> None:
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
    assert outcome.freshness == ""
    assert outcome.status == "denied"


def test_enrich_research_ack_http_401_freshness_unverified() -> None:
    class HttpErrorResearchPort:
        def search(self, query: str) -> list[ResearchSnippet]:
            del query
            raise AdapterHttpError(401)

    decision = classify_owner_task("Do competitor research on Acme")
    ack = ack_for_owner_task(decision)
    enriched, outcome = enrich_research_ack(
        ack,
        HttpErrorResearchPort(),
        query="Do competitor research on Acme",
        kill_switch=False,
    )
    assert enriched == ack
    assert outcome.status == "unauthorized"
    assert outcome.freshness == "unverified"


@pytest.mark.asyncio
async def test_inbound_research_freshness_persisted() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        port = RecordingMessagePort()
        await process_inbound_texts(
            provider="whatsapp",
            channel=Channel.WHATSAPP,
            items=[
                {
                    "id": "res.fresh.inbound.1",
                    "from": OWNER_FRESH_RESEARCH_INBOUND_PHONE,
                    "text": "Do competitor research on Acme",
                    "source": "audio",
                }
            ],
            store=store,
            port=port,
            kill_switch=False,
            owner_ids={OWNER_FRESH_RESEARCH_INBOUND_PHONE},
            sheets=FakeSheetsPort(),
            meta_ads=DisabledMetaAdsPort(),
            research=FakeResearchPort(SAMPLE_SNIPPETS),
        )
        db.commit()
        row = store.get_tool_run("res.fresh.inbound.1:tool:research_search")
        assert row is not None
        assert row.freshness == "cached"
        assert row.status == "ok"
        dumped = json.dumps(
            {
                "tool": row.tool,
                "status": row.status,
                "result_count": row.result_count,
                "freshness": row.freshness,
            }
        ).lower()
        assert "http" not in dumped
        assert "acme" not in dumped
        assert "url" not in dumped
        event = store.get_canonical_event(
            provider="whatsapp",
            provider_event_id="res.fresh.inbound.1:tool:research_search",
        )
        assert event is not None
        payload = json.loads(event.payload_json)
        assert payload == {
            "tool": "research_search",
            "status": "ok",
            "result_count": 2,
        }
        assert "freshness" not in payload
        assert "Acme Corp Overview" not in json.dumps(payload)
    finally:
        db.close()
