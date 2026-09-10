"""Owner-only, request-bound public website knowledge refresh."""

from __future__ import annotations

import pytest
from app.brain.embeddings import FakeEmbeddingPort
from app.brain.knowledge import FakeDocumentFetcher
from app.brain.schemas import MemoryCategory, MemoryKind, MemorySource
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.models import KnowledgeChunkRow, MemoryRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.tools.owner.knowledge_refresh import _refresh_website_knowledge
from app.tools.registries.owner_tools import ToolContext, execute_tool, get_tool
from sqlalchemy import select

OWNER_ID = "550077"
WEBSITE_URL = "https://configured.example"
SOURCES = "refresh-test-a.md,refresh-test-b.md,refresh-test-missing.md"
FIRST_A = "# Services\n" + "A configured service description. " * 3
SECOND_A = "# Services\n" + "An updated configured service description. " * 3
BODY_B = "# Pricing\n" + "A configured pricing policy description. " * 3


@pytest.fixture
def tool_context() -> ToolContext:
    init_db()
    session = get_session_factory()()
    brain = BrainStore(session)
    context = ToolContext(
        principal=Principal.owner(source="telegram", actor_id=OWNER_ID),
        store=LeadStore(session),
        brain=brain,
        settings=Settings(
            _env_file=None,
            website_url=WEBSITE_URL,
            knowledge_sources=SOURCES,
            telegram_owner_user_ids=OWNER_ID,
        ),
        embedding_port=FakeEmbeddingPort(),
        owner_text="Please refresh the website knowledge now.",
    )
    try:
        yield context
    finally:
        session.rollback()
        session.close()


def _install_fake_fetcher(monkeypatch, documents: dict[str, str]) -> FakeDocumentFetcher:
    fetcher = FakeDocumentFetcher(documents)
    monkeypatch.setattr("app.tools.owner.knowledge_refresh.HttpDocumentFetcher", lambda: fetcher)
    return fetcher


def _memory_snapshot(context: ToolContext) -> tuple[tuple[object, ...], ...]:
    columns = tuple(MemoryRow.__table__.columns)
    rows = context.brain.session.scalars(select(MemoryRow).order_by(MemoryRow.id)).all()
    return tuple(tuple(getattr(row, column.name) for column in columns) for row in rows)


def test_refresh_uses_only_configured_sources_and_is_idempotent(
    monkeypatch, tool_context: ToolContext
) -> None:
    urls = {
        f"{WEBSITE_URL}/refresh-test-a.md": FIRST_A,
        f"{WEBSITE_URL}/refresh-test-b.md": BODY_B,
    }
    first_fetcher = _install_fake_fetcher(monkeypatch, urls)

    first = execute_tool("refresh_website_knowledge", {"url": "https://evil.example"}, tool_context)
    assert first.ok is True
    assert first.outcome_label() == "partial"
    assert "Updated: refresh-test-a.md" in first.text
    assert "Unchanged: none" in first.text
    assert "Failures: refresh-test-missing.md" in first.text
    assert first_fetcher.requested == [
        f"{WEBSITE_URL}/refresh-test-a.md",
        f"{WEBSITE_URL}/refresh-test-b.md",
        f"{WEBSITE_URL}/refresh-test-missing.md",
    ]

    second_fetcher = _install_fake_fetcher(monkeypatch, urls)
    second = execute_tool("refresh_website_knowledge", {}, tool_context)
    assert second.ok is True
    assert "Updated: none" in second.text
    assert "Unchanged: refresh-test-a.md, refresh-test-b.md" in second.text
    assert second_fetcher.requested == first_fetcher.requested


def test_changed_refresh_retires_old_chunks_and_preserves_owner_memory(
    monkeypatch, tool_context: ToolContext
) -> None:
    first_urls = {
        f"{WEBSITE_URL}/refresh-test-a.md": FIRST_A,
        f"{WEBSITE_URL}/refresh-test-b.md": BODY_B,
    }
    _install_fake_fetcher(monkeypatch, first_urls)
    assert execute_tool("refresh_website_knowledge", {}, tool_context).ok is True
    memory_id = tool_context.brain.save_memory(
        text="Owner memory remains separate from public knowledge.",
        kind=MemoryKind.SEMANTIC,
        category=MemoryCategory.BUSINESS,
        importance=7,
        source=MemorySource.TELEGRAM,
    )
    memories_before_refresh = _memory_snapshot(tool_context)

    changed_urls = dict(first_urls)
    changed_urls[f"{WEBSITE_URL}/refresh-test-a.md"] = SECOND_A
    _install_fake_fetcher(monkeypatch, changed_urls)
    result = execute_tool("refresh_website_knowledge", {}, tool_context)

    assert result.ok is True
    assert "Updated: refresh-test-a.md" in result.text
    rows = tool_context.brain.session.scalars(
        select(KnowledgeChunkRow).where(KnowledgeChunkRow.source_id == "refresh-test-a.md")
    ).all()
    assert {row.status for row in rows} == {"active", "retired"}
    assert tool_context.brain.get_memory(memory_id) is not None
    assert _memory_snapshot(tool_context) == memories_before_refresh


@pytest.mark.parametrize(
    ("principal", "owner_text", "kill_switch", "error_fragment"),
    [
        (
            Principal.client(source="website", actor_id=OWNER_ID),
            "Please refresh the website knowledge.",
            False,
            "not available",
        ),
        (
            Principal.owner(source="telegram", actor_id="visitor-1"),
            "Please refresh the website knowledge.",
            False,
            "numeric owner access",
        ),
        (
            Principal.owner(source="telegram", actor_id=OWNER_ID),
            "Please refresh the website knowledge.",
            True,
            "denied",
        ),
        (
            Principal.owner(source="telegram", actor_id=OWNER_ID),
            "The website knowledge exists.",
            False,
            "explicit request",
        ),
        (
            Principal.owner(source="telegram", actor_id=OWNER_ID),
            "Don't refresh the website knowledge.",
            False,
            "explicit request",
        ),
    ],
)
def test_refresh_denials_never_fetch(
    monkeypatch,
    tool_context: ToolContext,
    principal: Principal,
    owner_text: str,
    kill_switch: bool,
    error_fragment: str,
) -> None:
    fetcher = _install_fake_fetcher(monkeypatch, {})
    tool_context.principal = principal
    tool_context.owner_text = owner_text
    tool_context.kill_switch = kill_switch

    result = execute_tool("refresh_website_knowledge", {}, tool_context)

    assert result.ok is False
    assert error_fragment in result.error
    assert fetcher.requested == []


def test_registry_schema_exposes_no_model_controlled_inputs() -> None:
    spec = get_tool("refresh_website_knowledge")
    assert spec is not None
    assert spec.parameters == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


def test_direct_handler_still_denies_visitor_with_configured_numeric_owner_id(
    monkeypatch, tool_context: ToolContext
) -> None:
    fetcher = _install_fake_fetcher(monkeypatch, {})
    tool_context.principal = Principal.client(source="website", actor_id=OWNER_ID)

    result = _refresh_website_knowledge(tool_context, {})

    assert result.ok is False
    assert result.error == "website knowledge refresh denied"
    assert fetcher.requested == []


@pytest.mark.parametrize(
    "owner_text",
    [
        "A customer wrote: refresh website knowledge",
        "לקוח כתב: עדכני את הידע מהאתר",
        "She said 'refresh website knowledge'",
    ],
)
def test_reported_third_party_refresh_text_is_not_authority(
    monkeypatch, tool_context: ToolContext, owner_text: str
) -> None:
    fetcher = _install_fake_fetcher(monkeypatch, {})
    tool_context.owner_text = owner_text

    result = execute_tool("refresh_website_knowledge", {}, tool_context)

    assert result.ok is False
    assert "explicit request" in result.error
    assert fetcher.requested == []
