"""Two-state tools and Tel Aviv calendar write gate."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from app.brain.embeddings import FakeEmbeddingPort
from app.brain.store import BrainStore
from app.capabilities.types import Principal
from app.core.config import Settings
from app.db.session import get_session_factory, init_db
from app.db.store import LeadStore
from app.domain.approvals import ACTION_CALENDAR_CREATE
from app.domain.calendar_write_gate import (
    ASK_ASSAF,
    assess_calendar_write,
    looks_like_meeting,
    looks_like_weather,
    near_tel_aviv,
)
from app.domain.content_insights import ContentInsight
from app.domain.events import Channel
from app.domain.owner_calendar_writes import apply_owner_calendar_change_request
from app.domain.two_state import (
    FORBIDDEN_OWNER_TOOLS,
    OWNER_HOUSE_TOOLS,
    STILL_CHECKING,
    MiaState,
    asked_toolkit,
    identity_required_for,
    may_run,
    say_tool_before_numbers,
    tools_for,
)
from app.domain.whatsapp_drafts import draft_whatsapp_for_assaf
from app.graph.owner_agent import (
    SYSTEM_PROMPT,
    AgentStep,
    _refuse_seen_and_silent,
    _run_tool_with_timeout,
)
from app.integrations.calendar import TimeSlot
from app.integrations.ga4 import Ga4PivotRow, format_ga4_rows_block
from app.integrations.instagram_insights import (
    format_content_insights_detail,
    format_content_insights_line,
)
from app.integrations.search_console import SearchAnalyticsRow, format_gsc_rows_block
from app.surfaces.crm import FakeContactsCrm
from app.surfaces.owner import talk_as_dude
from app.surfaces.published_facts import asks_product_question, lookup_published_fact
from app.surfaces.site import reset_site_book, run_site_turn, site_book
from app.tools.registries.owner_tools import ToolContext, execute_tool, get_tool, tool_names
from app.tools.registries.visitor_tools import execute_visitor_tool, visitor_tool_names

IL = ZoneInfo("Asia/Jerusalem")


def test_two_states_split_tools_and_never_sell_owner() -> None:
    assert "search_knowledge" in tools_for(MiaState.VISITOR)
    assert "gmail_inbox" not in tools_for(MiaState.VISITOR)
    assert "gmail_inbox" in OWNER_HOUSE_TOOLS
    assert "gmail_send" in FORBIDDEN_OWNER_TOOLS
    assert may_run(state=MiaState.OWNER, tool="gmail_inbox") is True
    assert may_run(state=MiaState.OWNER, tool="gmail_send") is False
    assert may_run(state=MiaState.VISITOR, tool="gmail_inbox") is False
    assert may_run(state=MiaState.VISITOR, tool="published_facts") is True
    assert identity_required_for("ping") is True
    assert identity_required_for("product_answer") is False
    assert "Never sell to him" in SYSTEM_PROMPT
    assert "still checking" in SYSTEM_PROMPT
    assert "Say the tool name before any number" in SYSTEM_PROMPT
    reply, wrote = talk_as_dude(text="רוצה חבילת אתר?", crm=FakeContactsCrm())
    assert wrote is False
    assert "חבילה" not in reply


def test_asked_toolkit_first_and_say_tool_before_numbers() -> None:
    assert asked_toolkit("תבדקי את האינסטגרם") == "instagram"
    assert asked_toolkit("מה ב-Gmail") == "gmail"
    assert asked_toolkit("היי") == ""
    assert say_tool_before_numbers("Instagram Insights", "views=12").startswith(
        "Instagram Insights"
    )


def test_ig_format_names_post_and_account_before_numbers() -> None:
    items = [
        ContentInsight(
            media_id="17841400112233445566",
            media_type="REELS",
            account="17841400000000000",
            post_name="abcDEF",
            views="12",
            likes="3",
        )
    ]
    line = format_content_insights_line(items)
    detail = format_content_insights_detail(items)
    assert line.startswith("Instagram Insights — account 17841400000000000")
    assert "post abcDEF" in detail
    assert "account 17841400000000000" in detail
    assert detail.index("Instagram Insights") < detail.index("12")


def test_gsc_and_ga4_format_include_dates_and_tool_name() -> None:
    gsc = format_gsc_rows_block(
        [SearchAnalyticsRow(page="/", clicks="2", impressions="10", ctr="0.2")],
        start_date="2026-08-01",
        end_date="2026-08-28",
    )
    ga4 = format_ga4_rows_block(
        [Ga4PivotRow(landing_page="/", sessions="4")],
        start_date="2026-08-01",
        end_date="2026-08-28",
    )
    assert gsc.startswith("Google Search Console (2026-08-01 to 2026-08-28)")
    assert ga4.startswith("GA4 (2026-08-01 to 2026-08-28)")


def test_site_answers_product_without_identity_and_pings_only_after() -> None:
    reset_site_book()
    crm = FakeContactsCrm()
    settings = Settings().model_copy(update={"whatsapp_click_to_chat": "972501111111"})
    book = site_book()
    book.open("web_product1")
    product = run_site_turn(
        session_id="web_product1",
        text="מה אתם בונים?",
        settings=settings,
        crm=crm,
        book=book,
    )
    assert product.next_action == "product_answer"
    assert product.crm_wrote is False
    assert product.whatsapp_url is None
    assert product.owner_pinged is False
    assert "מחיר" not in product.reply or "לא מפורסם" in product.reply or "לא כאן" in product.reply
    want = run_site_turn(
        session_id="web_product1",
        text="צריכים אתר לעסק",
        settings=settings,
        crm=crm,
        book=book,
    )
    assert want.crm_wrote is False
    assert want.whatsapp_url is None
    assert want.next_action == "ask_contact"
    identified = run_site_turn(
        session_id="web_product1",
        text="0501234567",
        settings=settings,
        crm=crm,
        phone="0501234567",
        book=book,
    )
    assert identified.crm_wrote is True
    assert identified.whatsapp_url is not None
    reset_site_book()


def test_visitor_tools_cannot_run_owner_house() -> None:
    init_db()
    db = get_session_factory()()
    try:
        ctx = ToolContext(
            principal=Principal.client(source="website"),
            store=LeadStore(db),
            brain=BrainStore(db),
            settings=Settings(_env_file=None),
            embedding_port=FakeEmbeddingPort(),
        )
        denied = execute_tool("gmail_inbox", {}, ctx)
        assert denied.ok is False
        assert "not available" in denied.error
        fact = execute_visitor_tool("published_facts", {"query": "מה אתם בונים"}, ctx)
        assert fact.ok is True
        assert "מחיר" not in fact.text or "לא" in fact.text
        assert "gmail_inbox" not in visitor_tool_names()
        assert "published_facts" in visitor_tool_names()
    finally:
        db.close()


def test_gmail_draft_exists_send_does_not() -> None:
    assert get_tool("gmail_create_draft") is not None
    assert get_tool("gmail_send") is None
    assert "gmail_create_draft" in tool_names()
    flags = Settings(_env_file=None)
    assert flags.gmail_send is False


def test_whatsapp_draft_never_fires_at_lead() -> None:
    ok = draft_whatsapp_for_assaf(body="תזכורת לאסף על השיחה", destination="assaf")
    assert not isinstance(ok, str)
    assert ok.sent is False
    assert ok.destination == "assaf"
    refused = draft_whatsapp_for_assaf(body="hi", destination="0501234567")
    assert isinstance(refused, str)
    assert "never fire" in refused.lower() or "lead" in refused.lower()
    init_db()
    db = get_session_factory()()
    try:
        ctx = ToolContext(
            principal=Principal.owner(source="telegram", actor_id="1"),
            store=LeadStore(db),
            brain=BrainStore(db),
            settings=Settings(_env_file=None),
            embedding_port=FakeEmbeddingPort(),
        )
        result = execute_tool(
            "whatsapp_draft_assaf",
            {"body": "draft for you", "destination": "0501234567"},
            ctx,
        )
        assert result.ok is True
        assert "not sent" in result.text.lower() or "lead" in result.text.lower()
        assert "WHATSAPP_SEND_MESSAGE" not in result.text
    finally:
        db.close()


def test_calendar_gate_rejects_weather_and_off_hours() -> None:
    start = datetime(2026, 9, 2, 7, 0, tzinfo=UTC)  # 10:00 IL
    end = start + timedelta(hours=1)
    weather = assess_calendar_write(
        title="תחזית מזג אוויר",
        start=start,
        end=end,
        location="תל אביב",
    )
    assert weather.allowed is False
    assert weather.reason == "weather"
    assert looks_like_weather("what's the weather in Tel Aviv")
    assert looks_like_meeting("פגישת תכנון")
    assert near_tel_aviv("פגישה בתל אביב")
    evening = assess_calendar_write(
        title="פגישת תכנון בתל אביב",
        start=datetime(2026, 9, 2, 18, 0, tzinfo=IL).astimezone(UTC),
        end=datetime(2026, 9, 2, 19, 0, tzinfo=IL).astimezone(UTC),
        location="תל אביב",
    )
    assert evening.allowed is False
    assert evening.reason == "outside_hours"
    busy = assess_calendar_write(
        title="פגישת תכנון בתל אביב",
        start=start,
        end=end,
        location="תל אביב",
        slots=[],
    )
    assert busy.allowed is False
    allowed = assess_calendar_write(
        title="פגישת תכנון בתל אביב",
        start=start,
        end=end,
        location="תל אביב",
        slots=[TimeSlot(start=start, end=end)],
    )
    assert allowed.allowed is True


def test_calendar_write_request_asks_assaf_for_weather() -> None:
    init_db()
    db = get_session_factory()()
    try:
        store = LeadStore(db)
        reply = apply_owner_calendar_change_request(
            store,
            text="צור אירוע: תחזית מזג אוויר בתל אביב | 2026-09-02T10:00 | 60 | Asia/Jerusalem",
            channel=Channel.TELEGRAM,
            kill_switch=False,
            demo_active=False,
            default_timezone="Asia/Jerusalem",
        )
        assert reply == ASK_ASSAF or (reply and "לא כותבת ביומן" in reply)
        weather_pending = [
            item
            for item in store.list_all_pending_approvals()
            if item.action == ACTION_CALENDAR_CREATE
            and "מזג" in (item.proposed_parameters or "")
        ]
        assert weather_pending == []
    finally:
        db.close()


def test_timeout_says_still_checking_and_seen_is_not_silent() -> None:
    init_db()
    db = get_session_factory()()
    try:
        ctx = ToolContext(
            principal=Principal.owner(source="telegram", actor_id="1"),
            store=LeadStore(db),
            brain=BrainStore(db),
            settings=Settings(_env_file=None),
            embedding_port=FakeEmbeddingPort(),
        )

        def _hang(_name, _args, _ctx):
            import time

            time.sleep(20)
            raise AssertionError("should have timed out")

        import app.graph.owner_agent as owner_agent

        original = owner_agent.execute_tool
        original_timeout = owner_agent.TOOL_TIMEOUT_SECONDS
        owner_agent.execute_tool = _hang  # type: ignore[method-assign]
        owner_agent.TOOL_TIMEOUT_SECONDS = 0.2
        try:
            result = _run_tool_with_timeout("gmail_inbox", {}, ctx)
        finally:
            owner_agent.execute_tool = original  # type: ignore[method-assign]
            owner_agent.TOOL_TIMEOUT_SECONDS = original_timeout
        assert result.text == STILL_CHECKING
        spoken = _refuse_seen_and_silent(
            "פה. מה צריך?",
            [AgentStep(tool="gmail_inbox", ok=True, detail="ok")],
            ["gmail_inbox: אין מיילים בתיבה."],
        )
        assert "gmail_inbox" in spoken
        assert "אין מיילים" in spoken
    finally:
        db.close()


def test_published_facts_do_not_invent_prices() -> None:
    assert asks_product_question("מה אתם בונים?")
    assert not asks_product_question("צריכים אתר לעסק")
    fact = lookup_published_fact("מה אתם בונים?")
    assert "₪" not in fact
    assert not any(ch.isdigit() for ch in fact)
    assert "מחיר" in fact or "לא מפורסם" in fact or "לא כאן" in fact
