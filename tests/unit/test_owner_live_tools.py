"""Owner Telegram live-read tools: present, read-only, fail closed when disconnected."""

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
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
        research = execute_tool("research_search", {"query": "assafweb.com"}, ctx)
        assert research.ok is True
        assert "Not connected" in research.text
        missing = execute_tool("research_search", {"query": ""}, ctx)
        assert missing.ok is False
    finally:
        session.close()
