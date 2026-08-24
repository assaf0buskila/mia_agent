"""Lead lookup by stated name or headline. Never invent a name."""

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.core.config import get_settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.events import Channel
from app.domain.lead_reviews import format_lead_matches
from app.tools.registries.owner_tools import ToolContext, execute_tool


def _session():
    init_db()
    return get_session_factory()()


def test_find_leads_matches_name_headline_and_lists_ambiguous() -> None:
    session = _session()
    try:
        store = LeadStore(session)
        _, lead_a = store.open_channel_lead(channel=Channel.WEBSITE, external_id="sess-a")
        _, lead_b = store.open_channel_lead(channel=Channel.WEBSITE, external_id="sess-b")
        sales_a = store.get_sales(lead_a)
        sales_a.display_name = "דני"
        sales_a.headline = "מוכר שעונים"
        store.save_sales(sales_a)
        sales_b = store.get_sales(lead_b)
        sales_b.display_name = "דניאל"
        sales_b.headline = "קליניקה"
        store.save_sales(sales_b)
        session.commit()

        one = store.find_leads("שעונים")
        assert len(one) == 1
        assert one[0].lead_id == lead_a

        by_name = store.find_leads("דני")
        assert {item.lead_id for item in by_name} == {lead_a, lead_b}

        listed = format_lead_matches(store, "דני")
        assert "כמה לידים מתאימים" in listed
        assert lead_a in listed
        assert "דני" in listed

        missing = format_lead_matches(store, "יעל")
        assert "לא מצאתי ליד בשם הזה" in missing
        assert "לא ניחשתי" in missing

        ctx = ToolContext(
            store=store,
            brain=BrainStore(session),
            settings=get_settings(),
            embedding_port=FakeEmbeddingPort(),
        )
        tool = execute_tool("find_leads", {"query": "שעונים"}, ctx)
        assert tool.ok is True
        assert lead_a in tool.text
        assert "מוכר שעונים" in tool.text
    finally:
        session.close()


def test_find_leads_does_not_guess_from_empty_query() -> None:
    session = _session()
    try:
        store = LeadStore(session)
        assert store.find_leads("") == []
        assert store.find_leads("   ") == []
    finally:
        session.close()
