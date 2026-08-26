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
