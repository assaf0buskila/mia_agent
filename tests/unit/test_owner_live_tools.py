"""Owner Telegram live-read tools: present, read-only, fail closed when disconnected."""

from uuid import uuid4

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import get_settings
from app.db.models import OwnerNotificationRow
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.tools.registries.owner_tools import (
    ToolContext,
    execute_tool,
    get_tool,
    tool_names,
)
from sqlalchemy import select

_LIVE_READS = (
    "gmail_summary",
    "gmail_inbox",
    "gmail_search",
    "gmail_read",
    "find_leads",
    "seo_snapshot",
    "website_kpis",
    "linkedin_snapshot",
    "instagram_insights",
    "owner_system_audit",
    "research_search",
    "crm_search",
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
            analytics_rows=[SearchAnalyticsRow(page="/", impressions="10", clicks="1", ctr="0.1")]
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
        assert "Google Search Console" in seo.text
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
        assert denied_li.ok is False
        assert "not available in this state" in denied_li.error
        denied_seo = execute_tool("seo_snapshot", {}, denied_ctx)
        assert denied_seo.ok is False
        assert "not available in this state" in denied_seo.error
    finally:
        session.close()


def test_owner_website_kpis_uses_separate_page_and_query_reads_and_formats_metrics() -> None:
    from datetime import UTC, datetime

    from app.integrations.ga4 import FakeGa4Port, Ga4PivotRow
    from app.integrations.search_console import FakeSearchConsolePort, SearchAnalyticsRow

    class RecordingGsc(FakeSearchConsolePort):
        def __init__(self) -> None:
            super().__init__()
            self.calls: list[list[str]] = []

        def query_search_analytics(self, *, start_date, end_date, dimensions):
            del start_date, end_date
            self.calls.append(list(dimensions))
            if dimensions == ["page"]:
                return [
                    SearchAnalyticsRow(
                        page="https://assafweb.com/",
                        clicks="9",
                        impressions="100",
                        ctr="0.09",
                        position="4.2",
                    )
                ]
            return [
                SearchAnalyticsRow(
                    query="growth operator", clicks="3", impressions="30", ctr="0.1", position="2.1"
                )
            ]

    session = _session()
    try:
        ctx = _ctx(session)
        gsc = RecordingGsc()
        ctx.search_console = gsc
        ctx.ga4 = FakeGa4Port(
            pivot_rows=[Ga4PivotRow(landing_page="/", users="8", sessions="12", conversions="2")],
            conversion_events=["generate_lead"],
        )
        ctx.now = datetime(2026, 8, 28, tzinfo=UTC)
        result = execute_tool("website_kpis", {}, ctx)
        assert result.ok is True
        assert gsc.calls == [["page"], ["query"]]
        assert result.text.startswith("Google Search Console and GA4 (2026-07-31 to 2026-08-27)")
        assert "2026-07-31 to 2026-08-27" in result.text
        assert "users 8, sessions 12, conversions 2" in result.text
        assert "clicks 9, impressions 100, CTR 0.09, position 4.2" in result.text
        assert "growth operator" in result.text
        assert "GOOGLE_" not in result.text
    finally:
        session.close()


def test_owner_website_kpis_reports_partial_failure_and_empty_honestly() -> None:
    from app.domain.tools import AdapterHttpError
    from app.integrations.ga4 import FakeGa4Port
    from app.integrations.search_console import FakeSearchConsolePort

    class FailingGsc(FakeSearchConsolePort):
        def query_search_analytics(self, **_kwargs):
            raise AdapterHttpError(401)

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.ga4 = FakeGa4Port()
        ctx.search_console = FailingGsc()
        result = execute_tool("website_kpis", {}, ctx)
        assert result.ok is True
        assert "GA4 traffic: no rows returned" in result.text
        assert result.text.count("unavailable (unauthorized)") == 2
    finally:
        session.close()


def test_owner_website_kpis_preserves_zero_and_marks_only_missing_metrics() -> None:
    from datetime import UTC, datetime

    from app.integrations.ga4 import FakeGa4Port, Ga4PivotRow
    from app.integrations.search_console import FakeSearchConsolePort, SearchAnalyticsRow

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.ga4 = FakeGa4Port(
            pivot_rows=[
                Ga4PivotRow(
                    landing_page="/zero",
                    users="0",
                    sessions=None,
                    conversions="",
                )
            ]
        )
        ctx.search_console = FakeSearchConsolePort(
            analytics_rows=[
                SearchAnalyticsRow(
                    page="/zero",
                    query="zero query",
                    clicks="0",
                    impressions=None,
                    ctr="0.0",
                    position="",
                )
            ]
        )
        ctx.now = datetime(2026, 9, 10, tzinfo=UTC)

        result = execute_tool("website_kpis", {}, ctx)

        assert result.ok is True
        assert "/zero: users 0, sessions unavailable, conversions unavailable" in result.text
        assert "clicks 0, impressions unavailable, CTR 0.0, position unavailable" in result.text
        assert "clicks unavailable" not in result.text
        assert "CTR unavailable" not in result.text
    finally:
        session.close()






def test_owner_system_audit_reports_each_surface_without_a_blanket_provider_claim() -> None:
    """A broad owner request is one model tool call, not a two-call partial answer."""
    session = _session()
    try:
        ctx = _ctx(session)
        result = execute_tool("owner_system_audit", {}, ctx)
        assert result.ok is True
        assert "בדיקת מערכת מלאה" in result.text
        assert "מגבלת שתי קריאות" not in result.text
        for label in (
            "Gmail",
            "Calendar agenda (today)",
            "Calendar availability",
            "LinkedIn profile",
            "Instagram Insights",
            "AssafWeb SEO, GSC and GA4",
            "Google Sheets (גיליון מורשה)",
            "Hot leads",
            "Pending approvals",
            "Website conversations",
            "Daily brief",
            "New booked meetings",
        ):
            assert label in result.text
        assert "אין גיליון מורשה מוגדר" not in result.text
        assert "לא נבדקו ולא בוצעו" in result.text
    finally:
        session.close()


def test_owner_system_audit_keeps_unseen_booked_meetings_unconsumed() -> None:
    """The aggregate audit reports its inbox snapshot without acknowledging it."""
    from app.domain.owner.notifications import KIND_MEETING_BOOKED

    session = _session()
    try:
        ctx = _ctx(session)
        lead_id = f"owner-audit-{uuid4().hex}"
        ctx.store.upsert_owner_notification(
            kind=KIND_MEETING_BOOKED,
            lead_id=lead_id,
            scheduled_at="2026-09-01T09:00:00+00:00",
        )
        before_count = ctx.store.count_unseen_owner_notifications(
            kinds=(KIND_MEETING_BOOKED,)
        )
        notification = session.scalar(
            select(OwnerNotificationRow).where(
                OwnerNotificationRow.kind == KIND_MEETING_BOOKED,
                OwnerNotificationRow.lead_id == lead_id,
            )
        )
        assert notification is not None
        notification_id = notification.id
        assert notification.seen_at == ""

        result = execute_tool("owner_system_audit", {}, ctx)

        after_count = ctx.store.count_unseen_owner_notifications(
            kinds=(KIND_MEETING_BOOKED,)
        )
        session.expire_all()
        notification_after = session.get(OwnerNotificationRow, notification_id)
        assert result.ok is True
        assert "New booked meetings: נבדק: התקבלה תשובה" in result.text
        assert after_count == before_count
        assert notification_after is not None
        assert notification_after.seen_at == ""
    finally:
        session.rollback()
        session.close()


def test_owner_system_audit_reports_bounded_instagram_read_as_partial_empty() -> None:
    from app.integrations.instagram_insights import InstagramInsightBudgetExceeded

    class BudgetLimitedInstagramPort:
        def list_recent_insights(self, *, limit: int = 5):
            del limit
            raise InstagramInsightBudgetExceeded()

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.instagram_insights = BudgetLimitedInstagramPort()
        result = execute_tool("owner_system_audit", {}, ctx)
        assert result.ok is True
        assert "Instagram Insights: נבדק: אין נתונים בטווח שנבדק" in result.text
        assert "instagram insights returned nothing" in result.text.casefold()
    finally:
        session.close()


def test_owner_system_audit_reads_one_configured_sheet_preview_without_writing() -> None:
    from app.integrations.sheets import FakeSheetsPort

    session = _session()
    try:
        ctx = _ctx(session)
        sheets = FakeSheetsPort()
        sheets.owner_values[("mia-crm", "Contacts!A1:N20")] = [["שם", "טלפון"]]
        ctx.sheets = sheets
        ctx.settings = ctx.settings.model_copy(
            update={
                "sheets_spreadsheet_id": "mia-crm",
                "sheets_allowed_spreadsheet_ids": "aaa-other,mia-crm",
            }
        )
        result = execute_tool("owner_system_audit", {}, ctx)
        assert result.ok is True
        assert "Sheet values: שם | טלפון" in result.text
        assert "Lead ID" not in result.text
        assert sheets.owner_operations == []
    finally:
        session.close()
