"""Mia must quote a published price she actually holds, and invent none when she does not.

The ingest classifies a chunk as `KnowledgeCategory.PRICING` from its heading. The
answer path then threw that away and re-derived "is this a price" by substring, so a
priced chunk whose first 280 characters never said "מחיר" was treated as no price at
all — which is why Mia answered "there is no published price list" with pricing.md
already in the corpus.
"""

from __future__ import annotations

from app.brain.schemas import KnowledgeCategory, RetrievedItem
from app.surfaces.site_policy import (
    NO_PRICE_HE,
    PRICING_CATEGORY,
    PublishedFact,
    classify_site_intent,
    decide_site_turn,
    facts_from_knowledge_hits,
    is_pricing_fact,
    published_price_line,
)

ASK = "כמה עולה לבנות אתר?"
SITE = "https://assafweb.com/pricing"


def _fact(text: str, *, category: str = "", url: str = SITE) -> PublishedFact:
    return PublishedFact(text=text, url=url, category=category)


def _decide(text: str, facts: tuple[PublishedFact, ...]):
    return decide_site_turn(
        thought=text,
        language="he",
        has_contact=False,
        already_confirmed=False,
        selling_stopped=False,
        already_pinged=False,
        facts=facts,
    )


def test_the_category_matches_the_ingest_enum() -> None:
    assert PRICING_CATEGORY == KnowledgeCategory.PRICING.value


def test_a_price_question_is_still_classified_as_price() -> None:
    assert classify_site_intent(ASK) == "price"


def test_a_pricing_chunk_is_quoted() -> None:
    fact = _fact('בניית אתר תדמית מתחילה ב-6,500 ש"ח.', category=PRICING_CATEGORY)
    decision = _decide(ASK, (fact,))
    assert decision.action == "answer"
    assert "6,500" in decision.reply
    assert "assafweb.com" in decision.reply


def test_pricing_wording_without_any_keyword_is_still_quoted() -> None:
    """The exact regression: priced, but the text never says מחיר / price / cost."""
    text = 'חבילת ההתחלה היא 4,500 ש"ח וכוללת ליווי של חודש.'
    for banned in ("מחיר", "price", "cost", "תעריף", "עלות"):
        assert banned not in text
    fact = _fact(text, category=PRICING_CATEGORY)
    assert is_pricing_fact(fact) is True
    assert published_price_line((fact,)) != ""
    assert "4,500" in _decide(ASK, (fact,)).reply


def test_no_pricing_evidence_still_refuses_to_invent() -> None:
    decision = _decide(ASK, ())
    assert decision.action == "no_price"
    assert decision.reply == NO_PRICE_HE


def test_unrelated_numeric_content_is_not_a_price() -> None:
    """A portfolio count must never be quoted as a price."""
    fact = _fact("בנינו 37 אתרים בשנה האחרונה.", category=KnowledgeCategory.PORTFOLIO.value)
    assert is_pricing_fact(fact) is False
    assert published_price_line((fact,)) == ""
    assert _decide(ASK, (fact,)).action == "no_price"


def test_a_priced_chunk_from_another_host_is_never_quoted() -> None:
    """Host filtering must survive the category change."""
    fact = _fact(
        "Our starter plan is 9999 USD a month.",
        category=PRICING_CATEGORY,
        url="https://not-assafweb.example/pricing",
    )
    assert published_price_line((fact,)) == ""
    assert _decide(ASK, (fact,)).action == "no_price"


def test_the_keyword_fallback_still_works_without_a_category() -> None:
    """Chunks retrieved without a category must not regress."""
    fact = _fact('המחיר לדף נחיתה הוא 3,000 ש"ח.', category="")
    assert is_pricing_fact(fact) is True
    assert published_price_line((fact,)) != ""


def test_the_category_survives_retrieval_into_a_fact() -> None:
    hit = RetrievedItem(
        item_id="c1",
        text='חבילת ההתחלה היא 4,500 ש"ח.',
        origin="knowledge",
        source_ref=SITE,
        category=KnowledgeCategory.PRICING.value,
    )
    facts = facts_from_knowledge_hits([hit])
    assert facts and facts[0].category == PRICING_CATEGORY
    assert is_pricing_fact(facts[0]) is True


def test_a_retrieval_hit_without_a_category_is_handled() -> None:
    hit = RetrievedItem(item_id="c2", text="בונים אתרים.", origin="knowledge", source_ref=SITE)
    facts = facts_from_knowledge_hits([hit])
    assert facts and facts[0].category == ""
    assert is_pricing_fact(facts[0]) is False
