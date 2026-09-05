"""House Composio ports are bound and invoked. Health-true must not mean disconnected."""

from __future__ import annotations

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.owner.brain import bind_owner_house_ports
from app.integrations.calendar import ComposioCalendarPort, FakeCalendarAgendaPort
from app.integrations.composio_catalog import ComposioCatalog
from app.integrations.ga4 import ComposioGa4Port, FakeGa4Port
from app.integrations.gmail import ComposioGmailPort, DisabledGmailPort, FakeGmailPort
from app.integrations.instagram_insights import (
    ComposioInstagramInsightsPort,
    FakeInstagramInsightsPort,
)
from app.integrations.linkedin import ComposioLinkedInPort, FakeLinkedInPort, LinkedInProfile
from app.integrations.search_console import ComposioSearchConsolePort, FakeSearchConsolePort
from app.integrations.sheets import ComposioSheetsPort, DisabledSheetsPort, FakeSheetsPort
from app.tools.registries.owner_tools import ToolContext, execute_tool


def _house_settings() -> Settings:
    return Settings(
        _env_file=None,
        composio_api_key="k",
        composio_user_id="house-entity",
    )


def test_bind_owner_house_ports_uses_composio_adapters_not_disabled() -> None:
    settings = _house_settings()
    house = bind_owner_house_ports(settings)
    assert isinstance(house["gmail"], ComposioGmailPort)
    assert not isinstance(house["gmail"], DisabledGmailPort)
    assert isinstance(house["linkedin"], ComposioLinkedInPort)
    assert isinstance(house["instagram_insights"], ComposioInstagramInsightsPort)
    assert isinstance(house["search_console"], ComposioSearchConsolePort)
    assert isinstance(house["ga4"], ComposioGa4Port)
    assert isinstance(house["calendar"], ComposioCalendarPort)
    assert isinstance(house["sheets"], ComposioSheetsPort)
    assert not isinstance(house["sheets"], DisabledSheetsPort)
    assert house["gmail"]._user_id == settings.composio_user_id
    assert house["linkedin"]._user_id == settings.composio_user_id
    catalog = ComposioCatalog.from_settings(settings)
    assert catalog is not None
    assert catalog._user_id == settings.composio_user_id


def test_owner_tools_invoke_house_reads_instead_of_saying_disconnected() -> None:
    init_db()
    db = get_session_factory()()
    settings = _house_settings()
    try:
        ctx = ToolContext(
            principal=Principal.owner(source="telegram", actor_id="550077"),
            store=LeadStore(db),
            brain=BrainStore(db),
            settings=settings,
            embedding_port=FakeEmbeddingPort(),
            gmail=FakeGmailPort(),
            linkedin=FakeLinkedInPort(
                LinkedInProfile(name="Assaf Web", headline="Growth operator")
            ),
            instagram_insights=FakeInstagramInsightsPort(),
            search_console=FakeSearchConsolePort(),
            ga4=FakeGa4Port(),
            calendar_agenda=FakeCalendarAgendaPort(),
            sheets=FakeSheetsPort(),
        )
        inbox = execute_tool("gmail_inbox", {}, ctx)
        assert inbox.ok is True
        assert "Not connected" not in inbox.text
        linkedin = execute_tool("linkedin_snapshot", {}, ctx)
        assert linkedin.ok is True
        assert "Assaf Web" in linkedin.text
        assert "disconnected" not in linkedin.text.lower()
        instagram = execute_tool("instagram_insights", {}, ctx)
        assert instagram.ok is True
        assert "Not connected" not in instagram.text
        agenda = execute_tool("calendar_agenda", {"range": "today"}, ctx)
        assert agenda.ok is True
        assert "Not connected" not in agenda.text
        kpis = execute_tool("website_kpis", {}, ctx)
        assert kpis.ok is True
        assert "Not connected" not in kpis.text
    finally:
        db.close()
