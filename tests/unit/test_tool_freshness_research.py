import json

from app.domain.tools import AdapterHttpError
from app.integrations.research import (
    DisabledResearchPort,
    FakeResearchPort,
    ResearchSnippet,
    enrich_research_ack,
)

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
    enriched, outcome = enrich_research_ack(
        "", FakeResearchPort(SAMPLE_SNIPPETS), query="Acme", kill_switch=False
    )
    assert outcome.freshness == "cached"
    assert outcome.status == "ok"
    assert outcome.result_count == 2
    assert "מקורות ציבוריים" in enriched
    dumped = json.dumps(outcome.model_dump()).lower()
    assert "http" not in dumped
    assert "acme" not in dumped
    assert "acme.com" not in dumped
    assert "example.com" not in dumped
    assert "leading provider of widgets" not in dumped
    assert "independent review" not in dumped


def test_enrich_research_ack_disabled_freshness_unverified() -> None:
    enriched, outcome = enrich_research_ack(
        "", DisabledResearchPort(), query="Acme", kill_switch=False
    )
    assert enriched == ""
    assert outcome.freshness == "unverified"
    assert outcome.status == "empty"


def test_enrich_research_ack_kill_switch_freshness_empty() -> None:
    class RaisingResearchPort:
        def search(self, query: str) -> list[ResearchSnippet]:
            del query
            raise RuntimeError("must not call port when kill switch is on")

    enriched, outcome = enrich_research_ack(
        "", RaisingResearchPort(), query="Acme", kill_switch=True
    )
    assert enriched == ""
    assert outcome.freshness == ""
    assert outcome.status == "denied"


def test_enrich_research_ack_http_401_freshness_unverified() -> None:
    class HttpErrorResearchPort:
        def search(self, query: str) -> list[ResearchSnippet]:
            del query
            raise AdapterHttpError(401)

    enriched, outcome = enrich_research_ack(
        "", HttpErrorResearchPort(), query="Acme", kill_switch=False
    )
    assert enriched == ""
    assert outcome.status == "unauthorized"
    assert outcome.freshness == "unverified"
