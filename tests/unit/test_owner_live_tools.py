"""Owner Telegram live-read tools: present, read-only, fail closed when disconnected."""

import json
import unicodedata
from uuid import uuid4

import pytest
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


def test_owner_sheets_writes_require_current_explicit_request_and_are_idempotent() -> None:
    from app.integrations.sheets import FakeSheetsPort

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-allowed"}
        )
        args = {
            "spreadsheet_id": "sheet-allowed",
            "range": "KPI!A1",
            "values": [["value"]],
        }
        assert execute_tool("sheets_append", args, ctx).ok is False
        assert ctx.sheets.owner_operations == []

        ctx.owner_text = "What is the Sheet address?"
        assert execute_tool("sheets_append", args, ctx).ok is False
        assert ctx.sheets.owner_operations == []

        for unrelated in (
            "כתוב לי על השיטות הכי טובות",
            "כתוב לי שיטה מלאה",
            "הוסף פירוט על השיטה",
            "write me what is in the Sheet",
            "write down what the Sheet says",
            "כתוב לי מה יש בגיליון",
            "מה המלאי של גיליון גוגל",
            "מה ההכנסות של גיליון גוגל",
        ):
            ctx.owner_text = unrelated
            assert execute_tool("sheets_append", args, ctx).ok is False
        assert ctx.sheets.owner_operations == []

        ctx.owner_text = 'Please append "value" to sheet-allowed at KPI!A1 in the Sheet'
        first = execute_tool("sheets_append", args, ctx)
        second = execute_tool("sheets_append", args, ctx)
        assert first.ok is True
        assert second.ok is True
        assert len(ctx.sheets.owner_operations) == 1

        ctx.source_ref = "telegram:event-update"
        ctx.owner_text = 'Please update sheet-allowed range KPI!A1 with "value" in the Sheet'
        assert execute_tool("sheets_update", args, ctx).ok is True
        assert len(ctx.sheets.owner_operations) == 2

        ctx.source_ref = "telegram:event-hebrew"
        ctx.owner_text = 'הוסף את "value" לגיליון גוגל sheet-allowed בטווח KPI!A1'
        assert execute_tool("sheets_append", args, ctx).ok is True
        assert len(ctx.sheets.owner_operations) == 3

        ctx.source_ref = "telegram:event-2"
        assert execute_tool("sheets_append", args, ctx).ok is True
        assert len(ctx.sheets.owner_operations) == 4

        ctx.source_ref = ""
        assert execute_tool("sheets_update", args, ctx).ok is False
        assert len(ctx.sheets.owner_operations) == 4

        ctx.source_ref = "telegram:event-3"
        outside = {**args, "spreadsheet_id": "outside"}
        assert execute_tool("sheets_append", outside, ctx).ok is False
        assert len(ctx.sheets.owner_operations) == 4
    finally:
        session.close()


def test_owner_sheets_exact_hebrew_mutation_tokens_authorize_writes() -> None:
    from app.integrations.sheets import FakeSheetsPort

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-allowed"}
        )
        args = {
            "spreadsheet_id": "sheet-allowed",
            "range": "KPI!A1",
            "values": [["value"]],
        }
        for index, (owner_text, tool_name) in enumerate(
            (
                ('עדכן את "value" בגיליון גוגל sheet-allowed בטווח KPI!A1', "sheets_update"),
                ('הוסף "value" לגיליון גוגל sheet-allowed בטווח KPI!A1', "sheets_append"),
                ('מלא את KPI!A1 בגיליון גוגל sheet-allowed ב-"value"', "sheets_update"),
                ('הכנס "value" לגיליון גוגל sheet-allowed בטווח KPI!A1', "sheets_append"),
            ),
            start=1,
        ):
            ctx.source_ref = f"telegram:hebrew-token-{index}"
            ctx.owner_text = owner_text
            assert execute_tool(tool_name, args, ctx).ok is True
        assert len(ctx.sheets.owner_operations) == 4
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
        assert "לא נבדק: אין גיליון מורשה מוגדר" in result.text
        assert "לא נבדקו ולא בוצעו" in result.text
    finally:
        session.close()


def test_owner_system_audit_keeps_unseen_booked_meetings_unconsumed() -> None:
    """The aggregate audit reports its inbox snapshot without acknowledging it."""
    from app.domain.owner_notify import KIND_MEETING_BOOKED

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
        sheets.owner_values[("mia-crm", "A1:J20")] = [["Lead ID", "Stage"]]
        ctx.sheets = sheets
        ctx.settings = ctx.settings.model_copy(
            update={
                "sheets_spreadsheet_id": "mia-crm",
                "sheets_allowed_spreadsheet_ids": "aaa-other,mia-crm",
            }
        )
        result = execute_tool("owner_system_audit", {}, ctx)
        assert result.ok is True
        assert "Sheet values: Lead ID | Stage" in result.text
        assert sheets.owner_operations == []
    finally:
        session.close()


def test_owner_sheets_twenty_second_whole_turn_grammar_is_effect_free_on_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review-22 prefixes cannot be discarded before a valid Sheets suffix."""
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-main"}
        )
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation

        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)

        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets

        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)
        args = {"spreadsheet_id": "sheet-main", "range": "KPI!A1", "values": [["x"]]}
        valid_append = 'Append "x" to sheet-main at KPI!A1 in the Sheet'
        valid_hebrew = 'הוסף "x" לגיליון sheet-main בטווח KPI!A1'
        denied_cases = 0
        allowed_cases = 0

        def effects() -> tuple[dict[str, int], int, int]:
            return (
                dict(calls),
                len(ctx.sheets.owner_operations),
                session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count(),
            )

        def deny(source_ref: str, owner_text: str, candidate_args: dict = args) -> None:
            nonlocal denied_cases
            before = effects()
            ctx.source_ref = source_ref
            ctx.owner_text = owner_text
            assert execute_tool("sheets_append", candidate_args, ctx).ok is False
            assert effects() == before
            denied_cases += 1

        def allow(source_ref: str, owner_text: str) -> None:
            nonlocal allowed_cases
            before = effects()
            ctx.source_ref = source_ref
            ctx.owner_text = owner_text
            assert execute_tool("sheets_append", args, ctx).ok is True
            assert execute_tool("sheets_append", args, ctx).ok is True
            after = effects()
            assert after[0] == {"claim": before[0]["claim"] + 2, "port": before[0]["port"] + 2}
            assert after[1:] == (before[1] + 1, before[2] + 1)
            allowed_cases += 1

        # All 24 effectful review-22 prefix classes: nine mixed-script operation
        # lookalikes, three readable-sentinel lookalikes, seven split operations, and
        # five structural or multilingual phrases.
        for prefix, request in (
            ("Appеnd blue;", valid_append), ("aԁd blue;", valid_append),
            ("updatе blue;", valid_append), ("fiӏl blue;", valid_append),
            ("entеr blue;", valid_append), ("Αppend blue;", valid_append),
            ("αdd blue;", valid_append), ("Appеnd כחול;", valid_hebrew),
            ("הוѕף blue;", valid_append), ("CΕLL;", valid_append),
            ("ΙD;", valid_append), ("TΑRGET;", valid_append),
            ("App end blue;", valid_append), ("App-end blue;", valid_append),
            ("App.end blue;", valid_append), ("App/end blue;", valid_append),
            ("App_end blue;", valid_append), ("App\u00a0end blue;", valid_append),
            ("App\u202fend blue;", valid_append), ("change the blue cells;", valid_append),
            ("put blue there;", valid_append), ("blue;", valid_append),
            ("שנה כחול;", valid_hebrew), ("change כחול;", valid_append),
        ):
            deny(f"telegram:review22-prefix-{denied_cases}", f"{prefix} {request}")

        for suffix in ("KPI!B2; ", '"blue"; ', ""):
            owner_text = f"{suffix}{valid_append}" if suffix else f"{valid_append} nonsense"
            deny(f"telegram:review22-boundary-{denied_cases}", owner_text)

        for prefix in ("Ⓐppend blue;", "ﬃ blue;", "App\u00adend blue;", "A\u034fppend blue;"):
            deny(f"telegram:review22-compat-prefix-{denied_cases}", f"{prefix} {valid_append}")

        deny(
            "telegram:review22-fullwidth-id",
            'Append "x" to ｓｈｅｅｔ－ｍａｉｎ at KPI!A1 in the Sheet',
        )
        deny(
            "telegram:review22-fullwidth-target",
            'Append "x" to sheet-main at ＫＰＩ！Ａ１ in the Sheet',
        )
        deny(
            "telegram:review22-compat-literal",
            'Append "ｅ\u0301" to sheet-main at KPI!A1 in the Sheet',
            {**args, "values": [["é"]]},
        )

        for source_ref, owner_text in (
            ("telegram:review22-bare", valid_append),
            ("telegram:review22-please", f"Please {valid_append}"),
            ("telegram:review22-record", f"Please record this now: {valid_append}"),
            ("telegram:review22-alufa", f"אַלּוּפָה {valid_hebrew}"),
        ):
            allow(source_ref, owner_text)
        for source_ref, verb in (
            ("telegram:review22-fullwidth-verb", "Ａｐｐｅｎｄ"),
            ("telegram:review22-math-verb", "𝐀𝐩𝐩𝐞𝐧𝐝"),
            ("telegram:review22-marked-verb", "App\u034fend"),
            ("telegram:review22-soft-hyphen-verb", "App\u00adend"),
        ):
            allow(source_ref, valid_append.replace("Append", verb, 1))

        assert denied_cases == 34
        assert allowed_cases == 8
        assert denied_cases + allowed_cases == 42
    finally:
        session.close()


def test_owner_sheets_fifteenth_review_controls_fail_closed_and_overlap_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Residual controls, incomplete JSON cells, and overlapping IDs never mutate Sheets."""
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-allowed,KPI!A1"}
        )
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation
        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)
        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets
        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)
        args = {"spreadsheet_id": "sheet-allowed", "range": "KPI!A1", "values": [["x"]]}

        for mark in ("\u200d", "\u200c", "\u200e", "\u034f", "\u0301"):
            ctx.source_ref = "telegram:marked-" + str(ord(mark))
            ctx.owner_text = f'Append "x" to sheet-allowed at KPI!A1 and B{mark}2 in the Sheet'
            assert execute_tool("sheets_append", args, ctx).ok is False
        for bad in ('Append "x" and "\\q" to sheet-allowed at KPI!A1 in the Sheet',
                    'Append "x" and "a\nb" to sheet-allowed at KPI!A1 in the Sheet',
                    'Append "x" and 123 to sheet-allowed at KPI!A1 in the Sheet',
                    'Append "x" and true to sheet-allowed at KPI!A1 in the Sheet'):
            ctx.source_ref = "telegram:bad-cell"
            ctx.owner_text = bad
            assert execute_tool("sheets_append", args, ctx).ok is False
        assert calls == {"claim": 0, "port": 0}
        assert session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count() == 0

        ctx.source_ref = "telegram:overlap"
        ctx.owner_text = 'Append "x" to sheet-allowed at KPI!A1 in the Sheet'
        assert execute_tool("sheets_append", args, ctx).ok is True
        assert execute_tool("sheets_append", args, ctx).ok is True
        assert calls == {"claim": 2, "port": 2}
        assert len(ctx.sheets.owner_operations) == 1
    finally:
        session.close()


def test_owner_sheets_write_binds_id_range_and_every_literal_before_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-allowed"}
        )
        args = {
            "spreadsheet_id": "sheet-allowed",
            "range": "KPI!A1:B1",
            "values": [["red", "blue"]],
        }
        monkeypatch.setattr(
            ctx.store,
            "claim_operation",
            lambda **_kwargs: pytest.fail("unbound Sheets write must not claim"),
        )
        monkeypatch.setattr(
            owner_tools,
            "_owner_sheets_port",
            lambda _ctx: pytest.fail("unbound Sheets write must not construct a port"),
        )
        for owner_text in (
            'Please append "red" and "blue" to the Sheet',  # missing ID and range
            'Please append "red" and "blue" to sheet-allowed in the Sheet',  # missing range
            'Please append "red" and "blue" to sheet-allowed at KPI!A2:B2 in the Sheet',
            'Please append "red" to sheet-allowed at KPI!A1:B1 in the Sheet',  # missing value
            'הוסף "red" ו-"blue" לגיליון גוגל sheet-allowed בטווח KPI!A2:B2',
        ):
            ctx.owner_text = owner_text
            result = execute_tool("sheets_append", args, ctx)
            assert result.ok is False
            assert "spreadsheet id and range" in result.error
        for invented_args, owner_text in (
            (
                {**args, "range": "KPI!A1", "values": [["1"]]},
                "Please append to sheet-allowed at KPI!A1 in the Sheet",
            ),
            (
                {**args, "range": "KPI!A1", "values": [["update"]]},
                "Please update sheet-allowed at KPI!A1 in the Sheet",
            ),
        ):
            ctx.owner_text = owner_text
            result = execute_tool("sheets_append", invented_args, ctx)
            assert result.ok is False
            assert "JSON-quoted literal" in result.error
        for tool_name, invented_args, owner_text in (
            (
                "sheets_append",
                {**args, "range": "KPI!A1", "values": [["value"]]},
                'Please update sheet-allowed at KPI!A1 with "value" in the Sheet',
            ),
            (
                "sheets_update",
                {**args, "range": "KPI!A1", "values": [["value"]]},
                'Please append "value" to sheet-allowed at KPI!A1 in the Sheet',
            ),
            (
                "sheets_update",
                {**args, "range": "KPI!A1", "values": [["value"]]},
                'הכנס "value" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            ),
            (
                "sheets_append",
                {**args, "range": "KPI!A1", "values": [["value"]]},
                'Please append "value" to sheet-allowed-extra at KPI!A10 in the Sheet',
            ),
            (
                "sheets_update",
                {**args, "values": [["x", "x"]]},
                'Please update sheet-allowed at KPI!A1:B1 with "x" in the Sheet',
            ),
        ):
            ctx.owner_text = owner_text
            result = execute_tool(tool_name, invented_args, ctx)
            assert result.ok is False
        assert ctx.sheets.owner_operations == []
    finally:
        session.close()


def test_owner_sheets_semantic_binding_rejects_negation_conflicts_and_payload_mismatches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ambiguous proposal may build a port, claim an event, or call a provider."""
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-allowed"}
        )
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation

        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)

        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets

        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)
        update_args = {
            "spreadsheet_id": "sheet-allowed",
            "range": "KPI!A1:B1",
            "values": [["red", "blue"]],
        }
        append_args = {**update_args, "range": "KPI!A1", "values": [["x"]]}

        rejected = (
            (
                "sheets_append",
                append_args,
                'Do not append. Update sheet-allowed at KPI!A1 with "x" in the Sheet',
            ),
            (
                "sheets_append",
                append_args,
                'Don\'t append "x" to sheet-allowed at KPI!A1 in the Sheet',
            ),
            (
                "sheets_append",
                append_args,
                'Don’t append "x" to sheet-allowed at KPI!A1 in the Sheet',
            ),
            (
                "sheets_append",
                append_args,
                'Dont append "x" to sheet-allowed at KPI!A1 in the Sheet',
            ),
            (
                "sheets_append",
                append_args,
                'Please not append "x" to sheet-allowed at KPI!A1 in the Sheet',
            ),
            (
                "sheets_update",
                update_args,
                'Please not update sheet-allowed at KPI!A1:B1 with "red" and "blue" in the Sheet',
            ),
            (
                "sheets_append",
                append_args,
                'Please not to append "x" to sheet-allowed at KPI!A1 in the Sheet',
            ),
            (
                "sheets_append",
                append_args,
                'Please not ever append "x" to sheet-allowed at KPI!A1 in the Sheet',
            ),
            (
                "sheets_append",
                append_args,
                'Please do not ever append "x" to sheet-allowed at KPI!A1 in the Sheet',
            ),
            (
                "sheets_append",
                append_args,
                'Please never ever append "x" to sheet-allowed at KPI!A1 in the Sheet',
            ),
            (
                "sheets_update",
                update_args,
                'Please not to update sheet-allowed at KPI!A1:B1 with "red" and "blue" '
                "in the Sheet",
            ),
            (
                "sheets_update",
                update_args,
                'Please not ever fill sheet-allowed at KPI!A1:B1 with "red" and "blue" '
                "in the Sheet",
            ),
            (
                "sheets_update",
                update_args,
                'Please do not ever enter sheet-allowed at KPI!A1:B1 with "red" and "blue" '
                "in the Sheet",
            ),
            (
                "sheets_append",
                append_args,
                'אל תוסיף את "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            ),
            (
                "sheets_append",
                append_args,
                'לא להוסיף את "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            ),
            (
                "sheets_append",
                append_args,
                'אל תכניס את "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            ),
            (
                "sheets_append",
                append_args,
                'לא להכניס את "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            ),
            (
                "sheets_update",
                update_args,
                'אל תעדכן את "red" ו-"blue" בגיליון גוגל sheet-allowed בטווח KPI!A1:B1',
            ),
            (
                "sheets_update",
                update_args,
                'לא לעדכן את "red" ו-"blue" בגיליון גוגל sheet-allowed בטווח KPI!A1:B1',
            ),
            (
                "sheets_update",
                update_args,
                'אל תמלא את "red" ו-"blue" בגיליון גוגל sheet-allowed בטווח KPI!A1:B1',
            ),
            (
                "sheets_update",
                update_args,
                'לא למלא את "red" ו-"blue" בגיליון גוגל sheet-allowed בטווח KPI!A1:B1',
            ),
            (
                "sheets_update",
                {**update_args, "values": [["red"]]},
                'Please update sheet-allowed at KPI!A1:B1 with "red" and "blue" in the Sheet',
            ),
            (
                "sheets_append",
                append_args,
                'Please append "x" to sheet-allowed at KPI!A1 in the Sheet and update it',
            ),
            (
                "sheets_update",
                update_args,
                'Please update "red" and "blue" to sheet-allowed at KPI!A1:B1 in the Sheet '
                "and append them",
            ),
            (
                "sheets_append",
                append_args,
                'Please append "x" to sheet-allowed at KPI!A1 and "y" to sheet-allowed '
                "at KPI!A2 in the Sheet",
            ),
            (
                "sheets_update",
                {**update_args, "range": "KPI!A1:C1", "values": [["red", "blue", "green"]]},
                'Please update sheet-allowed at KPI!A1:C1 with "red" and "blue" in the Sheet',
            ),
            (
                "sheets_append",
                {**append_args, "values": [["   "]]},
                'Please append "   " to sheet-allowed at KPI!A1 in the Sheet',
            ),
        )
        for tool_name, args, owner_text in rejected:
            ctx.owner_text = owner_text
            result = execute_tool(tool_name, args, ctx)
            assert result.ok is False
            assert calls == {"claim": 0, "port": 0}
            assert ctx.sheets.owner_operations == []
            assert session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count() == 0

        # Correcting this same event produces one provider write; its exact replay does not.
        ctx.owner_text = 'Please append "x" to sheet-allowed at KPI!A1 in the Sheet'
        assert execute_tool("sheets_append", append_args, ctx).ok is True
        assert execute_tool("sheets_append", append_args, ctx).ok is True
        assert ctx.sheets.owner_operations == [("append", "sheet-allowed", "KPI!A1", [["x"]])]
        assert calls == {"claim": 2, "port": 2}

        ctx.source_ref = "telegram:spaced-literal"
        ctx.owner_text = 'Please append "x  y" to sheet-allowed at KPI!A1 in the Sheet'
        spaced_args = {**append_args, "values": [["x  y"]]}
        assert execute_tool("sheets_append", spaced_args, ctx).ok is True
        assert ctx.sheets.owner_operations[-1] == ("append", "sheet-allowed", "KPI!A1", [["x  y"]])

        ctx.source_ref = "telegram:quoted-negation"
        ctx.owner_text = 'Please append "not ever append" to sheet-allowed at KPI!A1 in the Sheet'
        quoted_negation_args = {**append_args, "values": [["not ever append"]]}
        assert execute_tool("sheets_append", quoted_negation_args, ctx).ok is True
        assert ctx.sheets.owner_operations[-1] == (
            "append",
            "sheet-allowed",
            "KPI!A1",
            [["not ever append"]],
        )
    finally:
        session.close()


def test_owner_sheets_bounded_negation_modifiers_deny_before_all_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary negated wording is not an authorization for any Sheets mutation verb."""
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-allowed"}
        )
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation

        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)

        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets

        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)
        args = {"spreadsheet_id": "sheet-allowed", "range": "KPI!A1", "values": [["x"]]}
        negators = (
            "do not even",
            "do not accidentally",
            "don't really",
            "never under any circumstances",
            "not yet",
            "do not mistakenly",
            "do not possibly",
            "do not unintentionally",
            "do not whimsically",
            "do not prematurely",
        )
        for verb, tool_name in (
            ("append", "sheets_append"),
            ("add", "sheets_append"),
            ("update", "sheets_update"),
            ("fill", "sheets_update"),
            ("enter", "sheets_update"),
        ):
            for negator in negators:
                ctx.owner_text = (
                    f'Please {negator} {verb} "x" to sheet-allowed at KPI!A1 in the Sheet'
                )
                result = execute_tool(tool_name, args, ctx)
                assert result.ok is False
                assert calls == {"claim": 0, "port": 0}
                assert ctx.sheets.owner_operations == []
                assert (
                    session.query(IdempotencyRow)
                    .filter_by(scope="owner_sheets_write")
                    .count()
                    == 0
                )

        ctx.source_ref = "telegram:positive-fill"
        ctx.owner_text = 'Please fill "x" to sheet-allowed at KPI!A1 in the Sheet'
        assert execute_tool("sheets_update", args, ctx).ok is True
        ctx.source_ref = "telegram:positive-hebrew"
        ctx.owner_text = 'הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1'
        assert execute_tool("sheets_append", args, ctx).ok is True
        ctx.source_ref = "telegram:quoted-negation-data"
        quoted_args = {**args, "values": [["do not even append"]]}
        ctx.owner_text = (
            'Please append "do not even append" to sheet-allowed at KPI!A1 in the Sheet'
        )
        assert execute_tool("sheets_append", quoted_args, ctx).ok is True

        # Hebrew letter boundaries deliberately leave punctuation, parentheses and maqaf as
        # separators while keeping the same syllables inside Hebrew words inert.
        ctx.source_ref = "telegram:embedded-hebrew-word"
        ctx.owner_text = 'אלופה הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1'
        assert execute_tool("sheets_append", args, ctx).ok is True
        assert calls == {"claim": 4, "port": 4}
        assert len(ctx.sheets.owner_operations) == 4

        ctx.source_ref = "telegram:pointed-negation-repair"
        assert unicodedata.category("\u034f") == "Mn"
        assert unicodedata.combining("\u034f") == 0
        assert unicodedata.category("\u0903") == "Mc"
        assert unicodedata.category("\u20dd") == "Me"
        assert unicodedata.category("\u200d") == "Cf"
        assert unicodedata.category("\u200e") == "Cf"
        for owner_text in (
            'Please do not under any circumstances ever append "x" to sheet-allowed at KPI!A1 '
            "in the Sheet",
            'אל תבצע שום פעולה בשום מצב עד להודעה אחרת ואז הכנס "x" לגיליון גוגל '
            "sheet-allowed בטווח KPI!A1",
            'Do not update the archive; please append "x" to sheet-allowed at KPI!A1 in the Sheet',
            'לא לעדכן את הארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'בבקשה,לא לעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'בבקשה (לא) לעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'בבקשה אל־תעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'בבקשה: לא לעדכן ארכיון — הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'לא\nהכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'לא ' + ('מילה ' * 40) + 'הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'אל תעדכן ארכיון. הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'בבקשה לֹא לעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'בבקשה אַל תעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'בבקשה לֹ֑א לעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'בבקשה אַ֑ל תעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'בבקשה ל\u034fא לעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'בבקשה א\u034fל תעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'בבקשה ל\ufe0fא לעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'בבקשה א\u0903ל תעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'בבקשה ל\u20ddא לעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            "בבקשה ל\u034f\u0903\u20ddֹא לעדכן ארכיון, אבל הכנס \"x\" לגיליון גוגל "
            "sheet-allowed בטווח KPI!A1",
            'בבקשה ל\u200dא לעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            'בבקשה א\u200eל תעדכן ארכיון, אבל הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1',
            "בבקשה ל\u2060\u034f\u200bא לעדכן ארכיון, אבל הכנס \"x\" לגיליון גוגל "
            "sheet-allowed בטווח KPI!A1",
        ):
            ctx.owner_text = owner_text
            result = execute_tool("sheets_append", args, ctx)
            assert result.ok is False
            assert calls == {"claim": 4, "port": 4}
            assert len(ctx.sheets.owner_operations) == 4
            assert session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count() == 4
        assert len(ctx.sheets.owner_operations) == 4

        # A rejected pointed prohibition does not consume the source event: its corrected
        # request reaches the provider once and its exact replay remains idempotent.
        ctx.owner_text = 'Please append "x" to sheet-allowed at KPI!A1 in the Sheet'
        assert execute_tool("sheets_append", args, ctx).ok is True
        assert execute_tool("sheets_append", args, ctx).ok is True
        assert calls == {"claim": 6, "port": 6}
        assert len(ctx.sheets.owner_operations) == 5

        # Pointed negators are inert inside a JSON-quoted cell literal, while letters inside
        # an ordinary Hebrew word are not a standalone prohibition.
        ctx.source_ref = "telegram:quoted-pointed-negation"
        pointed_args = {**args, "values": [["ל\u200dא"]]}
        ctx.owner_text = 'Please append "ל\u200dא" to sheet-allowed at KPI!A1 in the Sheet'
        assert execute_tool("sheets_append", pointed_args, ctx).ok is True
        ctx.source_ref = "telegram:pointed-embedded-hebrew"
        ctx.owner_text = 'אַלּוּפָה הכנס "x" לגיליון גוגל sheet-allowed בטווח KPI!A1'
        assert execute_tool("sheets_append", args, ctx).ok is True
        assert calls == {"claim": 8, "port": 8}
    finally:
        session.close()


def test_owner_sheets_write_binds_exactly_one_unquoted_target_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model cannot choose one target from more than one owner-specified target."""
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-allowed,sheet-other"}
        )
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation

        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)

        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets

        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)
        args = {"spreadsheet_id": "sheet-allowed", "range": "KPI!A1", "values": [["x"]]}
        for owner_text in (
            'Please append "x" to sheet-allowed at KPI!A1 or sheet-other at KPI!B1 in the Sheet',
            'Please append "x" to sheet-allowed at KPI!A1 or KPI!B1 in the Sheet',
            'Please append "x" to sheet-allowed at KPI!A1 or Other!A1 in the Sheet',
            'Please append "x" to sheet-allowed at KPI!A1, KPI!B1 in the Sheet',
            'Please append "x" to sheet-allowed at KPI!A1; KPI!B1 in the Sheet',
            'Please append "x" to sheet-allowed at KPI!A1 / KPI!B1 in the Sheet',
            'Please append "x" to sheet-allowed at KPI!A1 (KPI!B1) in the Sheet',
            'Please append "x" to sheet-allowed at KPI!A1\nKPI!B1 in the Sheet',
            'Please append "x" to sheet-allowed at KPI!A1, foo bar!B1 in the Sheet',
            'Please append "x" to sheet-allowed at KPI!A1; B1 in the Sheet',
            'Please append "x" to sheet-allowed at at KPI!A1 in the Sheet',
            'Please append "x" to sheet-allowed at range KPI!A1 in the Sheet',
            'Please append "x" to sheet-allowed range at KPI!A1 in the Sheet',
            'Please append "x" to sheet-allowed at, range KPI!A1 in the Sheet',
            'Please append "x" to sheet-allowed at (range KPI!A1) in the Sheet',
            'Please append "x" to sheet-allowed at,\nRaNgE KPI!A1 in the Sheet',
            'Please append "x" to sheet-allowed at בטווח KPI!A1 in the Sheet',
            'Please append "x" to sheet-allowed בטווח at KPI!A1 in the Sheet',
            'Please append "x" to sheet-allowed at range בטווח KPI!A1 in the Sheet',
            'Please append "x" to sheet-allowed בטווח, RaNgE KPI!A1 in the Sheet',
            'Please append "x" to sheet-allowed AT (בטווח KPI!A1) in the Sheet',
            'Please append "x" to sheet-allowed AT at KPI!A1 in the Sheet',
            'Please append "x" to sheet-allowed Range at KPI!A1 in the Sheet',
            'Please append "x" to sheet-allowed AT Range at KPI!A1 in the Sheet',
            'Please append "x" to sheet-allowed at: range KPI!A1 in the Sheet',
            'Please append "x" to sheet-allowed at! RANGE KPI!A1 in the Sheet',
            'Please append "x" to sheet-allowed at- RaNgE KPI!A1 in the Sheet',
            'Please append "x" to sheet-allowed at_ range KPI!A1 in the Sheet',
            'Please append "x" to sheet-allowed AT___ RaNgE KPI!A1 in the Sheet',
            'הוסף "x" לגיליון גוגל sheet-allowed בטווח_ at KPI!A1',
            'הוסף "x" לגיליון גוגל sheet-allowed בטווח: at KPI!A1',
            'הוסף "x" לגיליון גוגל sheet-allowed את\u0301\u200d 😀 [RaNgE KPI!A1]',
            'Please append "x" to sheet-allowed or sheet-other at KPI!A1 in the Sheet',
            'Please append "x" to "sheet-allowed" at "KPI!A1" in the Sheet',
            'Please append "x" to sheet-allowed-extra at KPI!A10 in the Sheet',
        ):
            ctx.owner_text = owner_text
            assert execute_tool("sheets_append", args, ctx).ok is False
            assert calls == {"claim": 0, "port": 0}
            assert ctx.sheets.owner_operations == []
            assert session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count() == 0

        for source_ref, introducer in (
            ("telegram:lowercase-introducer", "at"),
            ("telegram:uppercase-introducer", "RANGE"),
            ("telegram:mixedcase-introducer", "RaNgE"),
        ):
            ctx.source_ref = source_ref
            ctx.owner_text = f'Please append "x" to sheet-allowed {introducer} KPI!A1 in the Sheet'
            assert execute_tool("sheets_append", args, ctx).ok is True
            assert execute_tool("sheets_append", args, ctx).ok is True
        # An arbitrary word between the selected ID and range introducer is not
        # authorized scaffolding, even when target extraction remains unambiguous.
        ctx.source_ref = "telegram:underscore-word-bearing"
        ctx.owner_text = 'Please append "x" to sheet-allowed at_foo range KPI!A1 in the Sheet'
        assert execute_tool("sheets_append", args, ctx).ok is False
        assert ctx.sheets.owner_operations == [
            ("append", "sheet-allowed", "KPI!A1", [["x"]]),
            ("append", "sheet-allowed", "KPI!A1", [["x"]]),
            ("append", "sheet-allowed", "KPI!A1", [["x"]]),
        ]
        assert calls == {"claim": 6, "port": 6}
    finally:
        session.close()


def test_owner_sheets_spaced_tab_binding_rejects_suffix_selection_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A model cannot select a suffix tab from an owner-stated spaced tab target."""
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-allowed"}
        )
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation

        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)

        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets

        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)
        suffix_args = {"spreadsheet_id": "sheet-allowed", "range": "Bar!A1", "values": [["x"]]}
        for owner_text in (
            'Please append "x" to sheet-allowed at Foo Bar!A1 or Other Bar!A1 in the Sheet',
            'Please append "x" to sheet-allowed at Foo Bar!A1 in the Sheet',
            'Please append "x" to sheet-allowed at Foo Bar!A1 or Foo Bar!A1 in the Sheet',
            'Please append "x" to sheet-allowed at Foo Bar!A1 or Foo Bar!A2 in the Sheet',
            'Please append "x" to sheet-allowed at foo bar!A1 in the Sheet',
        ):
            ctx.owner_text = owner_text
            assert execute_tool("sheets_append", suffix_args, ctx).ok is False
            assert calls == {"claim": 0, "port": 0}
            assert ctx.sheets.owner_operations == []
            assert session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count() == 0

        ctx.source_ref = "telegram:spaced-tab"
        ctx.owner_text = 'Please append "x" to sheet-allowed at Foo Bar!A1 in the Sheet'
        full_args = {**suffix_args, "range": "Foo Bar!A1"}
        assert execute_tool("sheets_append", full_args, ctx).ok is True
        assert execute_tool("sheets_append", full_args, ctx).ok is True
        assert ctx.sheets.owner_operations == [("append", "sheet-allowed", "Foo Bar!A1", [["x"]])]
        assert calls == {"claim": 2, "port": 2}
    finally:
        session.close()


def test_owner_sheets_target_extraction_ignores_quoted_literals_and_range_suffixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a second complete unquoted target makes an owner write ambiguous."""
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-allowed"}
        )
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation

        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)

        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets

        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)
        target = {"spreadsheet_id": "sheet-allowed"}

        for source_ref, owner_text, args in (
            (
                "telegram:quoted-target-value",
                'Please append "KPI!B1" and "x" to sheet-allowed at Foo Bar!A1:B1 in the Sheet',
                {**target, "range": "Foo Bar!A1:B1", "values": [["KPI!B1", "x"]]},
            ),
            (
                "telegram:single-spaced-tab",
                'Please append "x" and "y" to sheet-allowed at Foo Bar!A1:B1 in the Sheet',
                {**target, "range": "Foo Bar!A1:B1", "values": [["x", "y"]]},
            ),
            (
                "telegram:lowercase-spaced-tab",
                'Please append "x" and "y" to sheet-allowed at foo bar!A1:B1 in the Sheet',
                {**target, "range": "foo bar!A1:B1", "values": [["x", "y"]]},
            ),
            (
                "telegram:mixed-spaced-tab",
                'Please append "x" and "y" to sheet-allowed at fOo bAr!A1:B1 in the Sheet',
                {**target, "range": "fOo bAr!A1:B1", "values": [["x", "y"]]},
            ),
        ):
            ctx.source_ref = source_ref
            ctx.owner_text = owner_text
            result = execute_tool("sheets_append", args, ctx)
            assert result.ok is True

        assert calls == {"claim": 4, "port": 4}
        assert ctx.sheets.owner_operations == [
            ("append", "sheet-allowed", "Foo Bar!A1:B1", [["KPI!B1", "x"]]),
            ("append", "sheet-allowed", "Foo Bar!A1:B1", [["x", "y"]]),
            ("append", "sheet-allowed", "foo bar!A1:B1", [["x", "y"]]),
            ("append", "sheet-allowed", "fOo bAr!A1:B1", [["x", "y"]]),
        ]
        assert session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count() == 4
    finally:
        session.close()


def test_owner_sheets_rejects_lowercase_secondary_a1_targets_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every remaining unquoted A1-looking target is ambiguous, regardless of ASCII case."""
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-allowed"}
        )
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation

        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)

        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets

        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)
        args = {
            "spreadsheet_id": "sheet-allowed",
            "range": "Foo Bar!A1",
            "values": [["x"]],
        }
        for owner_text in (
            'Please append "x" to sheet-allowed at Foo Bar!A1 plus b2 in the Sheet',
            'Please append "x" to sheet-allowed at Foo Bar!A1 plus Other!b2 in the Sheet',
            'Please append "x" to sheet-allowed at Foo Bar!A1 plus other bar!b2 in the Sheet',
            'Please append "x" to sheet-allowed at Foo Bar!A1 plus oThEr BaR!b2:c3 in the Sheet',
            'Please append "x" to sheet-allowed at Foo Bar!A1 plus $B$2 in the Sheet',
            'Please append "x" to sheet-allowed at Foo Bar!A1 plus $B2 in the Sheet',
            'Please append "x" to sheet-allowed at Foo Bar!A1 plus B$2 in the Sheet',
            'Please append "x" to sheet-allowed at Foo Bar!A1 plus B:B in the Sheet',
            'Please append "x" to sheet-allowed at Foo Bar!A1 plus $B:$D in the Sheet',
            'Please append "x" to sheet-allowed at Foo Bar!A1 plus 2:2 in the Sheet',
            'Please append "x" to sheet-allowed at Foo Bar!A1 plus $2:$4 in the Sheet',
            'Please append "x" to sheet-allowed at Foo Bar!A1 plus Other!$B$2 in the Sheet',
            'Please append "x" to sheet-allowed at Foo Bar!A1 plus Other!2:2 in the Sheet',
            'Please append "x" to sheet-allowed at Foo Bar!A1 plus \'Other Tab\'!B:B in the Sheet',
        ):
            ctx.owner_text = owner_text
            assert execute_tool("sheets_append", args, ctx).ok is False
            assert calls == {"claim": 0, "port": 0}
            assert ctx.sheets.owner_operations == []
            assert session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count() == 0
    finally:
        session.close()


def test_owner_sheets_opaque_allowlisted_ids_do_not_mask_secondary_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact allowlisted IDs are opaque; separate A1 targets still fail before effects."""
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        opaque_ids = ("sheet-B2", "A1", "opaque_sheet-B2")
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": ",".join(opaque_ids)}
        )
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation

        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)

        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets

        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)
        for index, spreadsheet_id in enumerate(opaque_ids):
            ctx.source_ref = f"telegram:opaque-id-positive-{index}"
            ctx.owner_text = f'Please append "x" to {spreadsheet_id} at KPI!A1 in the Sheet'
            args = {"spreadsheet_id": spreadsheet_id, "range": "KPI!A1", "values": [["x"]]}
            assert execute_tool("sheets_append", args, ctx).ok is True
            assert execute_tool("sheets_append", args, ctx).ok is True

        assert calls == {"claim": 6, "port": 6}
        assert len(ctx.sheets.owner_operations) == 3
        assert session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count() == 3

        ctx.source_ref = "telegram:opaque-id-secondary-before"
        ctx.owner_text = 'Please append "x" to sheet-B2 at KPI!A1 plus Other!B2 in the Sheet'
        args = {"spreadsheet_id": "sheet-B2", "range": "KPI!A1", "values": [["x"]]}
        assert execute_tool("sheets_append", args, ctx).ok is False
        ctx.source_ref = "telegram:opaque-id-secondary-after"
        ctx.owner_text = 'Please append "x" to Other!B2 then sheet-B2 at KPI!A1 in the Sheet'
        assert execute_tool("sheets_append", args, ctx).ok is False
        assert calls == {"claim": 6, "port": 6}
        assert len(ctx.sheets.owner_operations) == 3
        assert session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count() == 3
    finally:
        session.close()


def test_owner_sheets_a1_like_opaque_id_may_bind_once_but_not_hide_targets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One raw opaque ID is allowed; each extra A1-looking reference is still unsafe."""
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(update={"sheets_allowed_spreadsheet_ids": "B2,A1"})
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation

        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)

        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets

        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)
        args = {"spreadsheet_id": "B2", "range": "KPI!A1", "values": [["x"]]}

        ctx.source_ref = "telegram:bare-opaque-id-once"
        ctx.owner_text = 'Please append "x" to B2 at KPI!A1 in the Sheet'
        assert execute_tool("sheets_append", args, ctx).ok is True
        assert execute_tool("sheets_append", args, ctx).ok is True
        assert calls == {"claim": 2, "port": 2}
        assert len(ctx.sheets.owner_operations) == 1
        assert session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count() == 1

        for source_ref, owner_text in (
            (
                "telegram:bare-opaque-id-before",
                'Please append "x" to B2 then B2 at KPI!A1 in the Sheet',
            ),
            (
                "telegram:bare-opaque-id-after",
                'Please append "x" to B2 at KPI!A1 then B2 in the Sheet',
            ),
            (
                "telegram:bare-opaque-id-qualified",
                'Please append "x" to B2 at KPI!A1 plus Other!B2 in the Sheet',
            ),
        ):
            ctx.source_ref = source_ref
            ctx.owner_text = owner_text
            assert execute_tool("sheets_append", args, ctx).ok is False
            assert calls == {"claim": 2, "port": 2}
            assert len(ctx.sheets.owner_operations) == 1
            assert session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count() == 1

        # A bare selected range equal to the opaque ID is a valid single target: the
        # target overlap is excluded from the one-ID-occurrence count.
        ctx.source_ref = "telegram:opaque-id-equals-bare-target"
        ctx.owner_text = 'Please append "x" to A1 at A1 in the Sheet'
        selected_args = {"spreadsheet_id": "A1", "range": "A1", "values": [["x"]]}
        assert execute_tool("sheets_append", selected_args, ctx).ok is True
        assert execute_tool("sheets_append", selected_args, ctx).ok is True
        assert calls == {"claim": 4, "port": 4}
        assert len(ctx.sheets.owner_operations) == 2
        assert session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count() == 2
    finally:
        session.close()


def test_owner_sheets_write_literal_binding_preserves_raw_json_codepoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Owner cell literals are exact JSON strings, never Unicode-normalized authorization."""
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-allowed"}
        )
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation

        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)

        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets

        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)
        target = {"spreadsheet_id": "sheet-allowed", "range": "KPI!A1"}

        for source_ref, owner_value, argument_value in (
            ("telegram:decomposed-to-precomposed", "e\u0301", "é"),
            ("telegram:precomposed-to-decomposed", "é", "e\u0301"),
            ("telegram:fullwidth-to-ascii", "Ａ", "A"),
            ("telegram:variation-to-plain", "x\ufe0f", "x"),
            ("telegram:control-to-plain", "x\u200d", "x"),
        ):
            ctx.source_ref = source_ref
            ctx.owner_text = (
                f'Please append {json.dumps(owner_value, ensure_ascii=False)} to sheet-allowed '
                "at KPI!A1 in the Sheet"
            )
            result = execute_tool(
                "sheets_append", {**target, "values": [[argument_value]]}, ctx
            )
            assert result.ok is False
            assert calls == {"claim": 0, "port": 0}
            assert ctx.sheets.owner_operations == []
            assert session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count() == 0

        # The rejected source event remains available for the literal the owner actually gave.
        ctx.source_ref = "telegram:literal-correction"
        ctx.owner_text = 'Please append "e\\u0301" to sheet-allowed at KPI!A1 in the Sheet'
        assert execute_tool("sheets_append", {**target, "values": [["é"]]}, ctx).ok is False
        assert calls == {"claim": 0, "port": 0}
        exact_args = {**target, "values": [["e\u0301"]]}
        assert execute_tool("sheets_append", exact_args, ctx).ok is True
        assert execute_tool("sheets_append", exact_args, ctx).ok is True
        assert calls == {"claim": 2, "port": 2}
        assert ctx.sheets.owner_operations == [
            ("append", "sheet-allowed", "KPI!A1", [["e\u0301"]])
        ]
    finally:
        session.close()


def test_owner_sheets_prevalidates_policy_and_arguments_before_port_or_claim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid writes must not reserve an idempotency operation or build an adapter."""
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-allowed"}
        )
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation

        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)

        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets

        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)
        owner = Principal.owner(source="test")
        valid_args = {
            "spreadsheet_id": "sheet-allowed",
            "range": "KPI!A1",
            "values": [["x"]],
        }

        def assert_rejected(
            owner_text: str,
            args: dict,
            *,
            kill_switch: bool = False,
            principal: Principal = owner,
        ) -> None:
            ctx.owner_text = owner_text
            ctx.kill_switch = kill_switch
            ctx.principal = principal
            result = execute_tool("sheets_append", args, ctx)
            assert result.ok is False
            assert calls == {"claim": 0, "port": 0}
            assert session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count() == 0

        assert_rejected(
            'Please append "x" to outside at KPI!A1 in the Sheet',
            {**valid_args, "spreadsheet_id": "outside"},
        )
        assert_rejected(
            'Please append "x" to sheet-allowed at KPI!B2:A1 in the Sheet',
            {**valid_args, "range": "KPI!B2:A1"},
        )
        assert_rejected(
            "Please append "
            + " ".join(['\"x\"'] * 11)
            + " to sheet-allowed at KPI!A1:J1 in the Sheet",
            {**valid_args, "range": "KPI!A1:J1", "values": [["x"] * 11]},
        )
        assert_rejected(
            'Please append "=SUM(A:A)" to sheet-allowed at KPI!A1 in the Sheet',
            {**valid_args, "values": [["=SUM(A:A)"]]},
        )
        assert_rejected(
            'Please append "" to sheet-allowed at KPI!A1 in the Sheet',
            {**valid_args, "values": [[""]]},
        )
        for non_string in (1, None, [], {}):
            assert_rejected(
                'Please append "x" to sheet-allowed at KPI!A1 in the Sheet',
                {**valid_args, "values": [[non_string]]},
            )
        assert_rejected(
            'Please append "x" to sheet-allowed at KPI!A1 in the Sheet',
            valid_args,
            kill_switch=True,
        )
        assert_rejected(
            'Please append "x" to sheet-allowed at KPI!A1 in the Sheet',
            valid_args,
            principal=Principal.client(source="website"),
        )

        # The exact source event remains usable after the rejected proposal is corrected.
        ctx.principal = owner
        ctx.kill_switch = False
        ctx.owner_text = 'Please append "x" to sheet-allowed at KPI!A1 in the Sheet'
        first = execute_tool("sheets_append", valid_args, ctx)
        second = execute_tool("sheets_append", valid_args, ctx)
        assert first.ok is True
        assert second.ok is True
        assert calls == {"claim": 2, "port": 2}
        assert len(ctx.sheets.owner_operations) == 1
    finally:
        session.close()


def test_owner_sheets_write_accepts_exact_english_binding() -> None:
    from app.integrations.sheets import FakeSheetsPort

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-allowed"}
        )
        ctx.owner_text = (
            'Please append "red" and "blue" to sheet-allowed at KPI!A1:B1 in the Sheet'
        )
        result = execute_tool(
            "sheets_append",
            {
                "spreadsheet_id": "sheet-allowed",
                "range": "KPI!A1:B1",
                "values": [["red", "blue"]],
            },
            ctx,
        )
        assert result.ok is True
        assert len(ctx.sheets.owner_operations) == 1
    finally:
        session.close()


def test_owner_sheets_adapter_errors_return_tool_results_without_retrying_append() -> None:
    from app.domain.tools import AdapterHttpError
    from app.integrations.sheets import FakeSheetsPort

    class FailingSheetsPort(FakeSheetsPort):
        def __init__(self) -> None:
            super().__init__()
            self.append_attempts = 0

        def read_values(self, *, spreadsheet_id: str, a1_range: str) -> list[list[str]]:
            del spreadsheet_id, a1_range
            raise AdapterHttpError(401)

        def append_values(
            self, *, spreadsheet_id: str, a1_range: str, values: list[list[str]]
        ) -> None:
            del spreadsheet_id, a1_range, values
            self.append_attempts += 1
            raise AdapterHttpError(503)

    session = _session()
    try:
        ctx = _ctx(session)
        port = FailingSheetsPort()
        ctx.sheets = port
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-allowed"}
        )
        read = execute_tool(
            "sheets_read", {"spreadsheet_id": "sheet-allowed", "range": "KPI!A1"}, ctx
        )
        assert read.ok is False
        assert read.error == "Sheets read unavailable (unauthorized)"

        ctx.owner_text = 'Append "x" to sheet-allowed at KPI!A1 in the Sheet'
        args = {"spreadsheet_id": "sheet-allowed", "range": "KPI!A1", "values": [["x"]]}
        first = execute_tool("sheets_append", args, ctx)
        second = execute_tool("sheets_append", args, ctx)
        assert first.ok is False
        assert first.error == "Sheets write unavailable (retryable)"
        assert second.ok is True
        assert "already handled" in second.text
        assert port.append_attempts == 1
    finally:
        session.close()


def test_owner_sheets_explicit_scalar_cell_grammar_is_counted_and_narrow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-2026,KPI2!A123"}
        )
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation

        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)

        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets

        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)
        args = {"spreadsheet_id": "sheet-2026", "range": "KPI2!A123", "values": [["x"]]}
        for owner_text in (
            'Append 123 and "x" to sheet-2026 at KPI2!A123 in the Sheet',
            'Append "x" plus -1.2e3 to sheet-2026 at KPI2!A123 in the Sheet',
            'הוסף 123 ו-"x" לגיליון sheet-2026 בטווח KPI2!A123',
            'הוסף "x" ו-123 לגיליון sheet-2026 בטווח KPI2!A123',
            'הוסף "x" ו־null לגיליון sheet-2026 בטווח KPI2!A123',
            'Append "x" and "\\q" to sheet-2026 at KPI2!A123 in the Sheet',
        ):
            ctx.source_ref = "telegram:scalar-negative"
            ctx.owner_text = owner_text
            assert execute_tool("sheets_append", args, ctx).ok is False
            assert calls == {"claim": 0, "port": 0}
            assert ctx.sheets.owner_operations == []
        assert calls == {"claim": 0, "port": 0}
        assert session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count() == 0

        # Numeric-looking IDs/targets and quoted numeric cell text stay valid and exact.
        ctx.source_ref = "telegram:numeric-positive"
        positive_args = {**args, "values": [["123"]]}
        ctx.owner_text = 'Append "123" to sheet-2026 at KPI2!A123 in the Sheet'
        assert execute_tool("sheets_append", positive_args, ctx).ok is True
        assert execute_tool("sheets_append", positive_args, ctx).ok is True
        assert calls == {"claim": 2, "port": 2}
        assert len(ctx.sheets.owner_operations) == 1
    finally:
        session.close()


def test_owner_sheets_complete_cell_binding_rejects_residual_json_cells(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every explicit extra JSON cell must fail before a claim, port, or provider write."""
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-main"}
        )
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation

        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)

        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets

        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)
        args = {"spreadsheet_id": "sheet-main", "range": "KPI!A1", "values": [["x"]]}

        def assert_rejected(tool_name: str, owner_text: str) -> None:
            ctx.owner_text = owner_text
            assert execute_tool(tool_name, args, ctx).ok is False
            assert calls == {"claim": 0, "port": 0}
            assert ctx.sheets.owner_operations == []
            assert session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count() == 0

        scalar_cells = (
            "-1",
            "+1",
            "0",
            "1.0",
            ".5",
            "1.",
            "1e3",
            "-1.2e-3",
            "true",
            "false",
            "null",
        )
        for scalar in scalar_cells:
            assert_rejected(
                "sheets_append",
                f'Append {scalar} plus "x" to sheet-main at KPI!A1 in the Sheet',
            )
            assert_rejected(
                "sheets_append",
                f'Append "x" with {scalar} to sheet-main at KPI!A1 in the Sheet',
            )
            assert_rejected(
                "sheets_append",
                f'הוסף "x" ו{scalar} לגיליון sheet-main בטווח KPI!A1',
            )

        for tool_name, verb in (
            ("sheets_append", "Append"),
            ("sheets_append", "add"),
            ("sheets_append", "הוסף"),
            ("sheets_append", "הכנס"),
            ("sheets_update", "Update"),
            ("sheets_update", "fill"),
            ("sheets_update", "enter"),
            ("sheets_update", "עדכן"),
            ("sheets_update", "מלא"),
        ):
            connector = "and" if verb.isascii() else "ו־"
            assert_rejected(
                tool_name,
                f'{verb} "x" {connector} 1 to sheet-main at KPI!A1 in the Sheet'
                if verb.isascii()
                else f'{verb} "x" {connector}1 לגיליון sheet-main בטווח KPI!A1',
            )

        for extra in ("[1]", '{"status":true}', "[]", "{}", "[1", '{"status":true'):
            for connector in ("and", "or", "plus", "with"):
                assert_rejected(
                    "sheets_append",
                    f'Append "x" {connector} {extra} to sheet-main at KPI!A1 in the Sheet',
                )
                assert_rejected(
                    "sheets_append",
                    f'Append {extra} {connector} "x" to sheet-main at KPI!A1 in the Sheet',
                )
            for connector in ("ו", "ו-", "ו־"):
                assert_rejected(
                    "sheets_append",
                    f'הוסף "x" {connector}{extra} לגיליון sheet-main בטווח KPI!A1',
                )
                assert_rejected(
                    "sheets_append",
                    f'הוסף {extra} {connector}"x" לגיליון sheet-main בטווח KPI!A1',
                )
    finally:
        session.close()


def test_owner_sheets_punctuation_and_layout_binding_are_effect_free_on_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cell grammar, order, and grid shape bind before every Sheets side effect."""
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-main,sheet-2026"}
        )
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation

        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)

        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets

        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)

        def assert_rejected(tool_name: str, owner_text: str, args: dict) -> None:
            before = (
                dict(calls),
                len(ctx.sheets.owner_operations),
                session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count(),
            )
            ctx.owner_text = owner_text
            assert execute_tool(tool_name, args, ctx).ok is False
            assert (
                dict(calls),
                len(ctx.sheets.owner_operations),
                session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count(),
            ) == before

        one_cell = {"spreadsheet_id": "sheet-main", "range": "KPI!A1", "values": [["x"]]}
        for tool_name, english, hebrew in (
            ("sheets_append", "Append", "הוסף"),
            ("sheets_append", "add", "הכנס"),
            ("sheets_update", "Update", "עדכן"),
            ("sheets_update", "fill", "מלא"),
            ("sheets_update", "enter", "עדכן"),
        ):
            for suffix in (
                "and: 1",
                "plus (1)",
                "with / 1",
                "or — 1",
                "and: [1]",
                "and: +true",
                "plus (--1)",
                "with -null",
            ):
                assert_rejected(
                    tool_name,
                    f'{english} "x" {suffix} to sheet-main at KPI!A1 in the Sheet',
                    one_cell,
                )
            for suffix in ("ו - 1", "ו ־ 1", "ו:1", "ו([1])", "ו:+true", "ו(--1)", "ו -null"):
                assert_rejected(
                    tool_name,
                    f'{hebrew} "x" {suffix} לגיליון sheet-main בטווח KPI!A1',
                    one_cell,
                )
            for pseudo_cell in ("NaN", "Infinity", "-Infinity", "None", "'x'"):
                assert_rejected(
                    tool_name,
                    f'{english} "x" and {pseudo_cell} to sheet-main at KPI!A1 in the Sheet',
                    one_cell,
                )
                assert_rejected(
                    tool_name,
                    f'{hebrew} "x" ו{pseudo_cell} לגיליון sheet-main בטווח KPI!A1',
                    one_cell,
                )

        for tool_name, english, hebrew in (
            ("sheets_append", "Append", "הוסף"),
            ("sheets_append", "add", "הכנס"),
            ("sheets_update", "Update", "עדכן"),
            ("sheets_update", "fill", "מלא"),
            ("sheets_update", "enter", "עדכן"),
        ):
            for connector in ("and", "or", "plus", "with", ",", ";"):
                for closer in ("]", "}", "])", "}}", "]}"):
                    separator = "" if connector in {",", ";"} else " "
                    assert_rejected(
                        tool_name,
                        f'{english} "x" {connector}{separator}{closer} '
                        "to sheet-main at KPI!A1 in the Sheet",
                        one_cell,
                    )
                    assert_rejected(
                        tool_name,
                        f'{english} {closer} {connector} "x" '
                        "to sheet-main at KPI!A1 in the Sheet",
                        one_cell,
                    )
            for connector in ("ו", "ו-", "ו־", ",", ";"):
                for closer in ("]", "}", "])", "}}", "]}"):
                    assert_rejected(
                        tool_name,
                        f'{hebrew} "x" {connector}\n{closer} '
                        "לגיליון sheet-main בטווח KPI!A1",
                        one_cell,
                    )
                    assert_rejected(
                        tool_name,
                        f'{hebrew} {closer} {connector}"x" '
                        "לגיליון sheet-main בטווח KPI!A1",
                        one_cell,
                    )

        arbitrary_cells = (
            "blue",
            "foo-bar",
            "undefined",
            "yes",
            "TRUEISH",
            "`x`",
            "???",
            "!!!",
            "...",
            "—",
            "כחול",
        )
        for tool_name, verb in (
            ("sheets_append", "Append"),
            ("sheets_append", "add"),
            ("sheets_update", "Update"),
            ("sheets_update", "fill"),
            ("sheets_update", "enter"),
        ):
            for connector in ("and", "or", "plus", "with", ",", ";"):
                separator = "" if connector in {",", ";"} else " "
                for extra in arbitrary_cells:
                    assert_rejected(
                        tool_name,
                        f'{verb} "x" {connector}{separator}{extra} '
                        "to sheet-main at KPI!A1 in the Sheet",
                        one_cell,
                    )
                    assert_rejected(
                        tool_name,
                        f'{verb} {extra} {connector} "x" '
                        "to sheet-main at KPI!A1 in the Sheet",
                        one_cell,
                    )
        for tool_name, verb in (
            ("sheets_append", "הוסף"),
            ("sheets_append", "הכנס"),
            ("sheets_update", "עדכן"),
            ("sheets_update", "מלא"),
        ):
            for connector in ("ו", "ו-", "ו־", ",", ";"):
                for extra in arbitrary_cells:
                    assert_rejected(
                        tool_name,
                        f'{verb} "x" {connector}{extra} '
                        "לגיליון sheet-main בטווח KPI!A1",
                        one_cell,
                    )
                    assert_rejected(
                        tool_name,
                        f'{verb} {extra} {connector}"x" '
                        "לגיליון sheet-main בטווח KPI!A1",
                        one_cell,
                    )

        two_cells = {
            "spreadsheet_id": "sheet-main",
            "range": "KPI!A1:B1",
            "values": [["a", "b"]],
        }
        assert_rejected(
            "sheets_append",
            'Append "a" and blue plus "b" to sheet-main at KPI!A1:B1 in the Sheet',
            two_cells,
        )
        assert_rejected(
            "sheets_update",
            'מלא "a" וundefined ו־"b" לגיליון sheet-main בטווח KPI!A1:B1',
            two_cells,
        )
        for seam in (
            "blue at",
            "כחול range",
            "??? at",
            "/ range",
            "( at",
        ):
            assert_rejected(
                "sheets_append",
                f'Append "x" to sheet-main {seam} KPI!A1 in the Sheet',
                one_cell,
            )
        for seam in ("blue", "כחול", "???", "/", "("):
            assert_rejected(
                "sheets_append",
                f'Append "x" to sheet-main at KPI!A1 {seam} in the Sheet',
                one_cell,
            )
            assert_rejected(
                "sheets_update",
                f'Update sheet-main range KPI!A1 {seam} with "x" in the Sheet',
                one_cell,
            )
        for raw in (
            "CELL",
            "cell",
            "CeLl",
            "ID",
            "id",
            "iD",
            "TARGET",
            "target",
            "TaRgEt",
            "\ue000",
            "\ue001",
            "\ue000C\ue001",
        ):
            assert_rejected(
                "sheets_append",
                f'Append "x" and {raw} to sheet-main at KPI!A1 in the Sheet',
                one_cell,
            )
            assert_rejected(
                "sheets_append",
                f'Append {raw} and "x" to sheet-main at KPI!A1 in the Sheet',
                one_cell,
            )
            assert_rejected(
                "sheets_update",
                f'Update sheet-main {raw} range KPI!A1 with "x" in the Sheet',
                one_cell,
            )
        for raw in ("\ue000", "\ue001", "\ue000C\ue001"):
            assert_rejected(
                "sheets_append",
                f'Append "first" and {raw} and "second" to sheet-main at KPI!A1:B1 in the Sheet',
                {
                    "spreadsheet_id": "sheet-main",
                    "range": "KPI!A1:B1",
                    "values": [["first", "second"]],
                },
            )

        def assert_success(source_ref: str, tool_name: str, owner_text: str, args: dict) -> None:
            ctx.source_ref = source_ref
            ctx.owner_text = owner_text
            assert execute_tool(tool_name, args, ctx).ok is True
            assert execute_tool(tool_name, args, ctx).ok is True

        assert_success(
            "telegram:layout-single",
            "sheets_append",
            'Append "x" to sheet-main at KPI!A1 in the Sheet',
            one_cell,
        )
        ordered_pair = {
            "spreadsheet_id": "sheet-main",
            "range": "KPI!A1:B1",
            "values": [["first", "second"]],
        }
        assert_success(
            "telegram:layout-pair",
            "sheets_append",
            'Append "first" and "second" to sheet-main at KPI!A1:B1 in the Sheet',
            ordered_pair,
        )
        grid = {
            "spreadsheet_id": "sheet-main",
            "range": "KPI!A1:B2",
            "values": [["a", "b"], ["c", "d"]],
        }
        assert_success(
            "telegram:layout-grid",
            "sheets_update",
            'Update "a", "b", "c", "d" to sheet-main at KPI!A1:B2 in the Sheet',
            grid,
        )
        duplicate_pair = {
            "spreadsheet_id": "sheet-main",
            "range": "KPI!A1:C1",
            "values": [["x", "x", "y"]],
        }
        assert_success(
            "telegram:layout-duplicates",
            "sheets_append",
            'Append "x", "x", "y" to sheet-main at KPI!A1:C1 in the Sheet',
            duplicate_pair,
        )
        assert_success(
            "telegram:layout-quoted-scalars",
            "sheets_append",
            'Append "true", "[1]", "{\\"a\\":1}", "123", "a\\\\b", "a\\"b", "]", "]}" '
            "to sheet-main at KPI!A1:H1 in the Sheet",
            {
                "spreadsheet_id": "sheet-main",
                "range": "KPI!A1:H1",
                "values": [["true", "[1]", '{"a":1}', "123", "a\\b", 'a"b', "]", "]}"]],
            },
        )
        assert_success(
            "telegram:layout-numeric-target",
            "sheets_append",
            'Append "123" to sheet-2026 at KPI2!A123 in the Sheet',
            {"spreadsheet_id": "sheet-2026", "range": "KPI2!A123", "values": [["123"]]},
        )
        assert_success(
            "telegram:layout-sentinel-looking-data",
            "sheets_append",
            'Append "CELL", "ID", "TARGET", "\ue000" to sheet-main at KPI!A1:D1 in the Sheet',
            {
                "spreadsheet_id": "sheet-main",
                "range": "KPI!A1:D1",
                "values": [["CELL", "ID", "TARGET", "\ue000"]],
            },
        )

        assert_rejected(
            "sheets_append",
            'Append "first" and "second" to sheet-main at KPI!A1:B1 in the Sheet',
            {**ordered_pair, "values": [["second", "first"]]},
        )
        assert_rejected(
            "sheets_update",
            'Update "a", "b", "c", "d" to sheet-main at KPI!A1:B2 in the Sheet',
            {**grid, "values": [["a", "c"], ["b", "d"]]},
        )
        assert_rejected(
            "sheets_append",
            'Append "x", "x", "y" to sheet-main at KPI!A1:C1 in the Sheet',
            {**duplicate_pair, "values": [["x", "y", "x"]]},
        )
        for values in ([["a", "b"], ["c"]], [["a", "b"]], [["a"], ["b"], ["c"], ["d"]]):
            assert_rejected(
                "sheets_update",
                'Update "a", "b", "c", "d" to sheet-main at KPI!A1:B2 in the Sheet',
                {**grid, "values": values},
            )
    finally:
        session.close()


def test_owner_sheets_twentieth_repair_binds_one_operation_clause_before_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Earlier mutation clauses and public sentinel collisions cannot be discarded."""
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-main,A1"}
        )
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation

        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)

        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets

        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)
        append_args = {"spreadsheet_id": "sheet-main", "range": "KPI!A1", "values": [["x"]]}
        update_args = dict(append_args)

        def effects() -> tuple[dict[str, int], int, int]:
            return (
                dict(calls),
                len(ctx.sheets.owner_operations),
                session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count(),
            )

        def assert_denied(tool_name: str, source_ref: str, owner_text: str) -> None:
            before = effects()
            ctx.source_ref = source_ref
            ctx.owner_text = owner_text
            args = append_args if tool_name == "sheets_append" else update_args
            assert execute_tool(tool_name, args, ctx).ok is False
            assert effects() == before

        # The 20 effectful clean-room classes: 5 EN + 4 HE + cross-language +
        # operation-prefix + 9 readable raw-placeholder collisions.
        for verb, tool_name in (
            ("Append", "sheets_append"), ("add", "sheets_append"),
            ("Update", "sheets_update"), ("fill", "sheets_update"), ("enter", "sheets_update"),
        ):
            assert_denied(
                tool_name, f"telegram:twentieth-en-{verb}",
                f'{verb} blue; {verb} "x" to sheet-main at KPI!A1 in the Sheet',
            )
        for verb, tool_name in (
            ("הוסף", "sheets_append"), ("הכנס", "sheets_append"),
            ("עדכן", "sheets_update"), ("מלא", "sheets_update"),
        ):
            assert_denied(
                tool_name, f"telegram:twentieth-he-{verb}",
                f'{verb} כחול; {verb} "x" לגיליון sheet-main בטווח KPI!A1',
            )
        assert_denied(
            "sheets_append", "telegram:twentieth-cross-language",
            'הוסף כחול; Append "x" to sheet-main at KPI!A1 in the Sheet',
        )
        assert_denied(
            "sheets_update", "telegram:twentieth-operation-prefix",
            'Update later; now update "x" to sheet-main at KPI!A1 in the Sheet',
        )
        for token in ("CELL", "cell", "CeLl", "ID", "id", "iD", "TARGET", "target", "TaRgEt"):
            assert_denied(
                "sheets_append", f"telegram:twentieth-readable-{token}",
                f'{token}; Append "x" to sheet-main at KPI!A1 in the Sheet',
            )

        for token in ("\ue000", "\ue001", "\ue000C\ue001", "\ue000I\ue001", "\ue000T\ue001"):
            assert_denied(
                "sheets_append", f"telegram:twentieth-pua-{ord(token[0])}-{len(token)}",
                f'{token}; Append "x" to sheet-main at KPI!A1 in the Sheet',
            )

        # Harmless ordinary prose before the sole operation stays valid and replay-safe.
        ctx.source_ref = "telegram:twentieth-harmless-prefix"
        ctx.owner_text = 'Please record this now: Append "x" to sheet-main at KPI!A1 in the Sheet'
        assert execute_tool("sheets_append", append_args, ctx).ok is True
        assert execute_tool("sheets_append", append_args, ctx).ok is True
        assert effects() == ({"claim": 2, "port": 2}, 1, 1)

        # Quoted public/private lookalikes remain data, but the exact order/grid/A1 binding holds.
        quoted_args = {
            "spreadsheet_id": "sheet-main",
            "range": "KPI!A1:D1",
            "values": [["CELL", "ID", "TARGET", "\ue000"]],
        }
        ctx.source_ref = "telegram:twentieth-quoted-lookalikes"
        ctx.owner_text = (
            'Append "CELL", "ID", "TARGET", "\ue000" to sheet-main at KPI!A1:D1 in the Sheet'
        )
        assert execute_tool("sheets_append", quoted_args, ctx).ok is True
        assert execute_tool("sheets_append", quoted_args, ctx).ok is True
        before = effects()
        reordered_args = {**quoted_args, "values": [["ID", "CELL", "TARGET", "\ue000"]]}
        assert execute_tool("sheets_append", reordered_args, ctx).ok is False
        assert effects() == before

        # ID and target are both A1, but Hebrew target-first gives each its grammatical role.
        equal_args = {"spreadsheet_id": "A1", "range": "A1", "values": [["x"]]}
        ctx.source_ref = "telegram:twentieth-hebrew-target-first-equal"
        ctx.owner_text = 'הוסף את A1 בגיליון A1 ב-"x"'
        assert execute_tool("sheets_append", equal_args, ctx).ok is True
        assert execute_tool("sheets_append", equal_args, ctx).ok is True
    finally:
        session.close()


def test_owner_sheets_twenty_first_unicode_security_view_is_effect_free_on_denial(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 126-case review matrix cannot hide an instruction or public sentinel."""
    from app.db.models import IdempotencyRow
    from app.integrations.sheets import FakeSheetsPort
    from app.tools.registries import owner_tools

    session = _session()
    try:
        ctx = _ctx(session)
        ctx.sheets = FakeSheetsPort()
        ctx.settings = ctx.settings.model_copy(
            update={"sheets_allowed_spreadsheet_ids": "sheet-main,A1"}
        )
        calls = {"claim": 0, "port": 0}
        real_claim = ctx.store.claim_operation

        def counted_claim(**kwargs):
            calls["claim"] += 1
            return real_claim(**kwargs)

        def counted_port(_ctx):
            calls["port"] += 1
            return ctx.sheets

        monkeypatch.setattr(ctx.store, "claim_operation", counted_claim)
        monkeypatch.setattr(owner_tools, "_owner_sheets_port", counted_port)
        standard_args = {"spreadsheet_id": "sheet-main", "range": "KPI!A1", "values": [["x"]]}
        denied_cases = 0
        valid_cases = 0

        def effects() -> tuple[dict[str, int], int, int]:
            return (
                dict(calls),
                len(ctx.sheets.owner_operations),
                session.query(IdempotencyRow).filter_by(scope="owner_sheets_write").count(),
            )

        def deny(
            tool_name: str, source_ref: str, owner_text: str, args: dict | None = None
        ) -> None:
            nonlocal denied_cases
            before = effects()
            ctx.source_ref = source_ref
            ctx.owner_text = owner_text
            assert execute_tool(tool_name, args or standard_args, ctx).ok is False
            assert effects() == before
            denied_cases += 1

        def allow(tool_name: str, source_ref: str, owner_text: str, args: dict) -> None:
            nonlocal valid_cases
            before = effects()
            ctx.source_ref = source_ref
            ctx.owner_text = owner_text
            assert execute_tool(tool_name, args, ctx).ok is True
            assert execute_tool(tool_name, args, ctx).ok is True
            after = effects()
            assert after[0] == {"claim": before[0]["claim"] + 2, "port": before[0]["port"] + 2}
            assert after[1:] == (before[1] + 1, before[2] + 1)
            valid_cases += 1

        operation_cases = (
            ("Append", "sheets_append", 'Append "x" to sheet-main at KPI!A1 in the Sheet'),
            ("add", "sheets_append", 'add "x" to sheet-main at KPI!A1 in the Sheet'),
            ("Update", "sheets_update", 'Update "x" to sheet-main at KPI!A1 in the Sheet'),
            ("fill", "sheets_update", 'fill "x" to sheet-main at KPI!A1 in the Sheet'),
            ("enter", "sheets_update", 'enter "x" to sheet-main at KPI!A1 in the Sheet'),
            ("הוסף", "sheets_append", 'הוסף "x" לגיליון sheet-main בטווח KPI!A1'),
            ("הכנס", "sheets_append", 'הכנס "x" לגיליון sheet-main בטווח KPI!A1'),
            ("עדכן", "sheets_update", 'עדכן "x" לגיליון sheet-main בטווח KPI!A1'),
            ("מלא", "sheets_update", 'מלא "x" לגיליון sheet-main בטווח KPI!A1'),
        )
        for marker in ("\u200d", "\u200c", "\u200e", "\u2060", "\ufeff", "\u034f", "\u0301"):
            for verb, tool_name, valid_clause in operation_cases:
                split = len(verb) // 2
                obscured = verb[:split] + marker + verb[split:]
                deny(
                    tool_name,
                    f"telegram:review21-verb-{ord(marker):04x}-{verb}",
                    f"{obscured} blue; {valid_clause}",
                )
        deny(
            "sheets_append",
            "telegram:review21-fullwidth-append",
            'Ａｐｐｅｎｄ blue; Append "x" to sheet-main at KPI!A1 in the Sheet',
        )

        for token in (
            "CE\u200dLL", "I\u200dD", "TAR\u200dGET", "CE\u034fLL", "I\u034fD", "TAR\u034fGET",
            "ＣＥＬＬ", "ＩＤ", "ＴＡＲＧＥＴ",
        ):
            deny(
                "sheets_append",
                f"telegram:review21-sentinel-{token.encode('unicode_escape').decode()}",
                f'{token}; Append "x" to sheet-main at KPI!A1 in the Sheet',
            )

        # Thirty pre-existing ambiguity/collision controls complete the 103 denials.
        for verb, tool_name, valid_clause in operation_cases:
            deny(tool_name, f"telegram:review21-duplicate-{verb}", f"{verb} blue; {valid_clause}")
        deny(
            "sheets_append", "telegram:review21-cross-language",
            'הוסף כחול; Append "x" to sheet-main at KPI!A1 in the Sheet',
        )
        deny(
            "sheets_append", "telegram:review21-negated-prefix",
            'Do not append blue; Append "x" to sheet-main at KPI!A1 in the Sheet',
        )
        for token in ("CELL", "cell", "CeLl", "ID", "id", "iD", "TARGET", "target", "TaRgEt"):
            deny(
                "sheets_append", f"telegram:review21-readable-{token}",
                f'{token}; Append "x" to sheet-main at KPI!A1 in the Sheet',
            )
        for token in ("\ue000", "\ue001", "\ue000C\ue001", "\ue000I\ue001", "\ue000T\ue001"):
            deny(
                "sheets_append", f"telegram:review21-private-{ord(token[0])}-{len(token)}",
                f'{token}; Append "x" to sheet-main at KPI!A1 in the Sheet',
            )
        for source_ref, text in (
            ("telegram:review21-equal-extra-id", 'Append "x" A1 at A1 A1 in the Sheet'),
            ("telegram:review21-equal-extra-target", 'Append "x" A1 at A1 range A1 in the Sheet'),
            ("telegram:review21-equal-reversed", 'Append A1 at A1 with "x" A1 in the Sheet'),
            ("telegram:review21-equal-he-extra", 'הוסף "x" בגיליון A1 בטווח A1 A1'),
            ("telegram:review21-equal-he-reversed", 'הוסף את A1 בגיליון A1 ב-"x" A1'),
        ):
            deny(
                "sheets_append", source_ref, text,
                {"spreadsheet_id": "A1", "range": "A1", "values": [["x"]]},
            )

        for source_ref, owner_text in (
            ("telegram:review21-bare", 'Append "x" to sheet-main at KPI!A1 in the Sheet'),
            ("telegram:review21-please", 'Please Append "x" to sheet-main at KPI!A1 in the Sheet'),
            ("telegram:review21-alufa", 'אלופה הוסף "x" לגיליון sheet-main בטווח KPI!A1'),
        ):
            allow("sheets_append", source_ref, owner_text, standard_args)
        allow(
            "sheets_append", "telegram:review21-harmless",
            'Please record this now: Append "x" to sheet-main at KPI!A1 in the Sheet',
            standard_args,
        )
        quoted_args = {
            "spreadsheet_id": "sheet-main", "range": "KPI!A1:E1",
            "values": [["Append", "Ａｐｐｅｎｄ", "CE\u200dLL", "ＣＥＬＬ", "\ue000"]],
        }
        allow(
            "sheets_append", "telegram:review21-quoted-data",
            'Append "Append", "Ａｐｐｅｎｄ", "CE\u200dLL", "ＣＥＬＬ", "\ue000" '
            "to sheet-main at KPI!A1:E1 in the Sheet",
            quoted_args,
        )
        for verb, tool_name, _standard_clause in operation_cases:
            equal_args = {"spreadsheet_id": "A1", "range": "A1", "values": [["x"]]}
            if verb.isascii():
                values_first = f'{verb} "x" A1 at A1 in the Sheet'
                target_first = f'{verb} A1 at A1 with "x" in the Sheet'
            else:
                values_first = f'{verb} "x" בגיליון A1 בטווח A1'
                target_first = f'{verb} את A1 בגיליון A1 ב-"x"'
            allow(tool_name, f"telegram:review21-equal-values-{verb}", values_first, equal_args)
            allow(tool_name, f"telegram:review21-equal-target-{verb}", target_first, equal_args)

        assert denied_cases == 103
        assert valid_cases == 23
        assert denied_cases + valid_cases == 126
    finally:
        session.close()
