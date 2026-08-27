from datetime import UTC, datetime, timedelta

from app.capabilities.calendar import calendar_handlers
from app.capabilities.leads import leads_handlers
from app.capabilities.mail import mail_handlers
from app.capabilities.policy import execute_capability
from app.capabilities.types import Principal
from app.domain.sales import FitLevel, PainLevel, SalesState
from app.integrations.calendar import FakeCalendarPort, TimeSlot
from app.integrations.gmail import FakeGmailPort, InboundEmail


def test_owner_mail_read_goes_through_capability_policy_composio_port() -> None:
    port = FakeGmailPort(
        {
            "msg_1": InboundEmail(
                message_id="msg_1",
                sender="lead@example.com",
                subject="Intro",
                text="We need a site",
                thread_id="th_1",
            )
        }
    )
    out = execute_capability(
        "mail.read",
        principal=Principal.owner(source="test"),
        args={"message_id": "msg_1"},
        handlers=mail_handlers(port),
    )
    assert out["found"] is True
    assert out["subject"] == "Intro"
    assert out["sender"] == "lead@example.com"
    assert "timestamp" in out
    assert "GMAIL_FETCH" not in str(out)


def test_owner_calendar_schedule_goes_through_capability_policy() -> None:
    start = datetime(2026, 8, 26, 8, 0, tzinfo=UTC)
    end = start + timedelta(hours=2)
    port = FakeCalendarPort([TimeSlot(start=start, end=end)])
    out = execute_capability(
        "calendar.get_schedule",
        principal=Principal.owner(source="test"),
        args={
            "time_min": start.isoformat(),
            "time_max": (start + timedelta(days=1)).isoformat(),
            "duration_minutes": 30,
            "timezone": "Asia/Jerusalem",
        },
        handlers=calendar_handlers(port),
    )
    assert out["count"] == 1
    assert out["slots"][0]["start"] == start.isoformat()
    assert "GOOGLECALENDAR" not in str(out)
    assert "FIND_FREE_SLOTS" not in str(out)


class _LeadStore:
    def list_sales_snapshots(self, *, limit: int = 20) -> list[SalesState]:
        del limit
        return [
            SalesState(
                lead_id="lead_abc123def456",
                fit=FitLevel.GOOD,
                pain_level=PainLevel.P3,
                workflow_known=True,
                headline="needs a site",
            )
        ]

    def list_hot_lead_ids(self) -> list[str]:
        return ["lead_abc123def456"]


def test_owner_leads_recent_goes_through_capability_policy() -> None:
    out = execute_capability(
        "leads.get_recent",
        principal=Principal.owner(source="test"),
        args={"limit": 8},
        handlers=leads_handlers(_LeadStore()),  # type: ignore[arg-type]
    )
    assert out["leads"][0]["lead_id"] == "lead_abc123def456"
    assert out["hot_ids"] == ["lead_abc123def456"]
    assert "transcript" not in str(out).lower()


class _EmptyBrain:
    def list_memories(self, **_kwargs: object) -> list:
        return []

    def memory_vectors(self, **_kwargs: object) -> list:
        return []

    def list_knowledge_chunks(self) -> list:
        return []

    def knowledge_vectors(self) -> list:
        return []


def test_owner_memory_and_knowledge_search_go_through_capability_policy() -> None:
    from app.brain.embeddings import DisabledEmbeddingPort
    from app.brain.retrieval import MemoryScoreWeights
    from app.capabilities.knowledge import knowledge_handlers
    from app.capabilities.memory import memory_handlers

    brain = _EmptyBrain()
    embeddings = DisabledEmbeddingPort()
    mem = execute_capability(
        "memory.search",
        principal=Principal.owner(source="test"),
        args={"query": "AssafWeb pricing"},
        handlers=memory_handlers(
            brain=brain,  # type: ignore[arg-type]
            embedding_port=embeddings,
            weights=MemoryScoreWeights(),
        ),
    )
    know = execute_capability(
        "knowledge.search",
        principal=Principal.owner(source="test"),
        args={"query": "pricing"},
        handlers=knowledge_handlers(brain=brain, embedding_port=embeddings),  # type: ignore[arg-type]
    )
    assert mem["hits"] == []
    assert know["hits"] == []


def test_owner_research_search_goes_through_capability_policy() -> None:
    from app.capabilities.research import research_handlers
    from app.integrations.research import FakeResearchPort, ResearchSnippet

    port = FakeResearchPort(
        [
            ResearchSnippet(
                title="AssafWeb",
                url="https://www.assafweb.com/",
                excerpt="sites",
            )
        ]
    )
    out = execute_capability(
        "research.search",
        principal=Principal.owner(source="test"),
        args={"query": "AssafWeb"},
        handlers=research_handlers(port),
    )
    assert out["count"] == 1
    assert out["hits"][0]["url"] == "https://www.assafweb.com/"
    assert "FIRECRAWL" not in str(out)
    assert port.last_query == "AssafWeb"


def test_owner_linkedin_profile_goes_through_capability_policy() -> None:
    from app.capabilities.linkedin import linkedin_handlers
    from app.integrations.linkedin import FakeLinkedInPort, LinkedInProfile

    port = FakeLinkedInPort(
        LinkedInProfile(name="Assaf Web", headline="Growth operator")
    )
    out = execute_capability(
        "linkedin.get_profile",
        principal=Principal.owner(source="test"),
        args={},
        handlers=linkedin_handlers(port),
    )
    assert out["found"] is True
    assert out["name"] == "Assaf Web"
    assert out["headline"] == "Growth operator"
    assert "LINKEDIN" not in str(out)
    assert "GET_MY_INFO" not in str(out)


def test_owner_search_console_and_analytics_go_through_capability_policy() -> None:
    from app.capabilities.analytics import analytics_handlers
    from app.capabilities.search_console import search_console_handlers
    from app.integrations.ga4 import FakeGa4Port, Ga4PivotRow
    from app.integrations.search_console import FakeSearchConsolePort, SearchAnalyticsRow

    gsc = FakeSearchConsolePort(
        analytics_rows=[
            SearchAnalyticsRow(page="/", impressions="10", clicks="1", ctr="0.1")
        ]
    )
    ga4 = FakeGa4Port(
        pivot_rows=[Ga4PivotRow(landing_page="/", sessions="4")],
        conversion_events=["generate_lead"],
    )
    search = execute_capability(
        "search_console.query",
        principal=Principal.owner(source="test"),
        args={
            "start_date": "2026-08-01",
            "end_date": "2026-08-28",
            "dimensions": ["page"],
        },
        handlers=search_console_handlers(gsc),
    )
    traffic = execute_capability(
        "analytics.get_traffic",
        principal=Principal.owner(source="test"),
        args={"start_date": "2026-08-01", "end_date": "2026-08-28"},
        handlers=analytics_handlers(ga4),
    )
    assert search["count"] == 1
    assert search["rows"][0]["page"] == "/"
    assert "GOOGLE_SEARCH_CONSOLE" not in str(search)
    assert traffic["count"] == 1
    assert traffic["conversions"] == ["generate_lead"]
    assert "GOOGLE_ANALYTICS" not in str(traffic)
    assert "RUN_PIVOT" not in str(traffic)
