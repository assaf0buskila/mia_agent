"""Owner Telegram live-read tools: present, read-only, fail closed when disconnected."""

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import get_settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.tools.registries.owner_tools import (
    ToolContext,
    execute_tool,
    get_tool,
    tool_names,
)

_LIVE_READS = (
    "gmail_summary",
    "gmail_inbox",
    "gmail_search",
    "gmail_read",
    "find_leads",
    "seo_snapshot",
    "linkedin_snapshot",
    "instagram_insights",
    "research_search",
)


def _session():
    init_db()
    return get_session_factory()()


def _ctx(session) -> ToolContext:
    return ToolContext(
        principal=Principal.owner(source="test"),
        store=LeadStore(session),
        brain=BrainStore(session),
        settings=get_settings(),
        embedding_port=FakeEmbeddingPort(),
        source_ref="telegram:test",
    )


def test_live_read_tools_are_registered_and_do_not_write() -> None:
    names = tool_names()
    for name in _LIVE_READS:
        assert name in names
        assert get_tool(name).writes_memory is False
    writers = [item for item in names if get_tool(item).writes_memory]
    assert writers == ["remember"]


def test_disconnected_live_reads_do_not_raise() -> None:
    session = _session()
    try:
        ctx = _ctx(session)
        for name in (
            "seo_snapshot",
            "linkedin_snapshot",
            "instagram_insights",
        ):
            result = execute_tool(name, {}, ctx)
            assert result.ok is True
            assert "Not connected" in result.text
        gmail = execute_tool("gmail_summary", {"query": "what's in my inbox"}, ctx)
        assert gmail.ok is True
        inbox = execute_tool("gmail_inbox", {}, ctx)
        assert inbox.ok is True
        assert "Not connected" in inbox.text
        research = execute_tool("research_search", {"query": "assafweb.com"}, ctx)
        assert research.ok is True
        assert "Not connected" in research.text
        missing = execute_tool("research_search", {"query": ""}, ctx)
        assert missing.ok is False
    finally:
        session.close()


def test_owner_linkedin_and_seo_tools_use_fake_ports() -> None:
    from app.integrations.ga4 import FakeGa4Port, Ga4PivotRow
    from app.integrations.linkedin import FakeLinkedInPort, LinkedInProfile
    from app.integrations.search_console import FakeSearchConsolePort, SearchAnalyticsRow
    from app.integrations.seo_audit import FakeSeoAuditPort, SeoAuditSnapshot

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.linkedin = FakeLinkedInPort(
            LinkedInProfile(name="Assaf Web", headline="Growth operator")
        )
        ctx.search_console = FakeSearchConsolePort(
            analytics_rows=[
                SearchAnalyticsRow(page="/", impressions="10", clicks="1", ctr="0.1")
            ]
        )
        ctx.ga4 = FakeGa4Port(
            pivot_rows=[Ga4PivotRow(landing_page="/", sessions="4")],
            conversion_events=["generate_lead"],
        )
        ctx.seo_audit = FakeSeoAuditPort(
            SeoAuditSnapshot(url="https://www.assafweb.com/", title="AssafWeb", h1_count=1)
        )
        linkedin = execute_tool("linkedin_snapshot", {}, ctx)
        assert linkedin.ok is True
        assert "Assaf Web" in linkedin.text
        assert "LINKEDIN_GET_MY_INFO" not in linkedin.text
        seo = execute_tool("seo_snapshot", {}, ctx)
        assert seo.ok is True
        assert "נתוני חיפוש" in seo.text
        assert "GOOGLE_SEARCH_CONSOLE" not in seo.text
        assert "GOOGLE_ANALYTICS" not in seo.text
        denied_ctx = ToolContext(
            principal=Principal.client(source="website"),
            store=ctx.store,
            brain=ctx.brain,
            settings=ctx.settings,
            embedding_port=ctx.embedding_port,
            linkedin=ctx.linkedin,
            search_console=ctx.search_console,
            ga4=ctx.ga4,
            seo_audit=ctx.seo_audit,
            source_ref="website:test",
        )
        denied_li = execute_tool("linkedin_snapshot", {}, denied_ctx)
        assert denied_li.ok is True
        assert "Assaf Web" not in denied_li.text
        denied_seo = execute_tool("seo_snapshot", {}, denied_ctx)
        assert denied_seo.ok is True
        assert "נתוני חיפוש" not in denied_seo.text
    finally:
        session.close()
