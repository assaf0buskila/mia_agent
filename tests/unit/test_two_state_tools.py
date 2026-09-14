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
from app.domain.content_insights import ContentInsight
from app.domain.events import Channel
from app.domain.meetings.write_gate import (
    ASK_ASSAF,
    assess_calendar_write,
    looks_like_meeting,
    looks_like_weather,
    near_tel_aviv,
)
from app.domain.owner.calendar_writes import apply_owner_calendar_change_request
from app.domain.two_state import (
    FORBIDDEN_OWNER_TOOLS,
    OWNER_HOUSE_TOOLS,
    MiaState,
    asked_toolkit,
    identity_required_for,
    may_run,
    say_tool_before_numbers,
    tools_for,
)
from app.graph.owner_agent import (
    SYSTEM_PROMPT,
    TOOL_DEADLINE_REPLY,
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
from app.integrations.sheets import FakeSheetsPort
from app.tools.registries.owner_tools import ToolContext, execute_tool, get_tool, tool_names

IL = ZoneInfo("Asia/Jerusalem")


def test_two_states_split_tools_and_never_sell_owner() -> None:
    assert "search_knowledge" in tools_for(MiaState.VISITOR)
    assert "gmail_inbox" not in tools_for(MiaState.VISITOR)
    assert "gmail_inbox" in OWNER_HOUSE_TOOLS
    assert "gmail_send" in FORBIDDEN_OWNER_TOOLS
    assert may_run(state=MiaState.OWNER, tool="gmail_inbox") is True
    assert may_run(state=MiaState.OWNER, tool="gmail_send") is False
    assert may_run(state=MiaState.VISITOR, tool="gmail_inbox") is False
    assert identity_required_for("ping") is True
    assert identity_required_for("product_answer") is False
    assert "Never sell to the owner" in SYSTEM_PROMPT


def test_asked_toolkit_first_and_say_tool_before_numbers() -> None:
    assert asked_toolkit("תבדקי את האינסטגרם") == "instagram"
    assert asked_toolkit("מה ב-Gmail") == "gmail"
    assert asked_toolkit("היי") == ""
    assert asked_toolkit("Sheets עדיין עובד?") == "sheets"
    assert asked_toolkit("גוגל שיטס") == "sheets"
    assert asked_toolkit("האקסל") == "sheets"
    assert asked_toolkit("Contacts") == "sheets"
    assert asked_toolkit("CRM") == "sheets"
    assert asked_toolkit("Google sheets?") == "sheets"
    assert say_tool_before_numbers("Instagram Insights", "views=12").startswith(
        "Instagram Insights"
    )


def test_asked_toolkit_explicit_platform_outranks_generic_word() -> None:
    # A generic word ("פוסט"/"post") alone still resolves to today's default (instagram).
    assert asked_toolkit("תכתבי פוסט") == "instagram"
    # But an explicitly named platform wins even when a generic word for another
    # toolkit ("פוסט") is also present in the sentence.
    assert asked_toolkit("תכתבי לי פוסט ללינקדאין") == "linkedin"
    assert asked_toolkit("post for LinkedIn") == "linkedin"
    assert asked_toolkit("פוסט לאינסטגרם") == "instagram"
    # Two platforms explicitly named at once: don't force either.
    assert asked_toolkit("פוסט ללינקדאין ולאינסטגרם") == ""
    assert asked_toolkit("instagram or linkedin post") == ""
    # More linkedin/instagram spellings (P3).
    assert asked_toolkit("תכתבי פוסט ללינקדין") == "linkedin"
    assert asked_toolkit("פוסט ללינקד אין בבקשה") == "linkedin"
    assert asked_toolkit("write a post for linked in") == "linkedin"
    assert asked_toolkit("תעלי לי סטורי לאינסטה") == "instagram"
    assert asked_toolkit("post it on insta") == "instagram"


def test_asked_toolkit_tie_rule_stays_scoped_to_instagram_and_linkedin() -> None:
    """The instagram/linkedin tie-break must not leak into unrelated toolkits.

    A loose substring in another toolkit's needles (e.g. "שיט" inside "שיטה"/"שיטת",
    a plain Hebrew word for "method") must never manufacture a false tie, and must
    never preempt a toolkit the plain ordered scan would have picked first.
    """
    # Calendar named first, instagram second: the ordered scan hits instagram's own
    # needle ("פוסט"/"אינסטגרם") before ever reaching calendar's "יומן" — unaffected
    # by the instagram/linkedin override, which never triggers without a linkedin name.
    assert asked_toolkit("תבדוק את היומן ותכין פוסט לאינסטגרם") == "instagram"
    # "שיטה" (method) contains "שיט" (the sheets abbreviation) only as a substring;
    # it must not manufacture a false instagram/sheets tie.
    assert asked_toolkit("פוסט לאינסטגרם בשיטה חדשה") == "instagram"
    # The ordered scan reaches "sheets" (via "sheet" in "cheat sheet") before it ever
    # considers instagram, so naming LinkedIn later in the sentence must not steal it.
    assert asked_toolkit("cheat sheet for a linkedin post") == "sheets"
    # "שיטת" (method-of) again collides on "שיט"; the generic "פוסט" default (today's
    # behaviour, no platform named explicitly) must win, not the sheets substring.
    assert asked_toolkit("שיטת עבודה לפוסט") == "instagram"


def test_ig_format_names_post_and_account_before_numbers() -> None:
    items = [
        ContentInsight(
            media_id="17841400112233445566",
            media_type="REELS",
            account="assafweb",
            post_name="Launch hook",
            caption="Launch hook",
            timestamp="2026-09-01T10:00:00+0000",
            permalink="https://instagram.com/p/abc",
            views="12",
            likes="3",
        )
    ]
    line = format_content_insights_line(items)
    detail = format_content_insights_detail(items)
    assert "assafweb" in line
    assert "post Launch hook" in detail
    assert "https://instagram.com/p/abc" in detail
    assert "2026-09-01T10:00:00+0000" in detail
    assert detail.index("Instagram Insights") < detail.index("12")
    anonymous = format_content_insights_detail(
        [
            ContentInsight(
                media_id="17841400112233445566",
                media_type="REELS",
                views="999",
            )
        ]
    )
    assert "API omitted post identity" in anonymous
    assert "999" not in anonymous


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


def test_gmail_draft_exists_send_does_not() -> None:
    assert get_tool("gmail_create_draft") is not None
    assert get_tool("gmail_send") is None
    assert "gmail_create_draft" in tool_names()
    flags = Settings(_env_file=None)
    assert flags.gmail_send is False


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
    assert looks_like_meeting("שיחת ייעוץ")
    assert near_tel_aviv("פגישה בתל אביב")
    assert near_tel_aviv("שיחת ייעוץ בזום")
    assert near_tel_aviv("פגישה בהרצליה")
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


def test_calendar_gate_requires_explicit_remote_medium_outside_tel_aviv() -> None:
    start = datetime(2026, 9, 2, 10, 0, tzinfo=IL).astimezone(UTC)
    end = start + timedelta(hours=1)

    paris_meeting = assess_calendar_write(
        title="Physical planning meeting",
        start=start,
        end=end,
        location="Paris",
        slots=[TimeSlot(start=start, end=end)],
    )
    hebrew_conversation = assess_calendar_write(
        title="שיחת ייעוץ",
        start=start,
        end=end,
        location="פריז",
        slots=[TimeSlot(start=start, end=end)],
    )
    assert paris_meeting.allowed is False
    assert paris_meeting.reason == "not_tel_aviv"
    assert hebrew_conversation.allowed is False
    assert hebrew_conversation.reason == "not_tel_aviv"

    zoom = assess_calendar_write(
        title="שיחת ייעוץ בזום",
        start=start,
        end=end,
        slots=[TimeSlot(start=start, end=end)],
    )
    google_meet = assess_calendar_write(
        title="Planning meeting",
        start=start,
        end=end,
        location="Google Meet",
        slots=[TimeSlot(start=start, end=end)],
    )
    assert zoom.allowed is True
    assert google_meet.allowed is True


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
            if item.action == ACTION_CALENDAR_CREATE and "מזג" in (item.proposed_parameters or "")
        ]
        assert weather_pending == []
    finally:
        db.close()


def test_timeout_reports_stopped_work_and_seen_is_not_silent() -> None:
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

            time.sleep(0.1)
            raise AssertionError("should have timed out")

        import app.graph.owner_agent as owner_agent

        original = owner_agent.execute_tool
        original_timeout = owner_agent.TOOL_TIMEOUT_SECONDS
        owner_agent.execute_tool = _hang  # type: ignore[method-assign]
        owner_agent.TOOL_TIMEOUT_SECONDS = 0.01
        try:
            result = _run_tool_with_timeout("gmail_inbox", {}, ctx)
        finally:
            owner_agent.execute_tool = original  # type: ignore[method-assign]
            owner_agent.TOOL_TIMEOUT_SECONDS = original_timeout
        assert result.text == TOOL_DEADLINE_REPLY
        spoken = _refuse_seen_and_silent(
            "פה. מה צריך?",
            [AgentStep(tool="gmail_inbox", ok=True, detail="ok")],
            ["gmail_inbox: אין מיילים בתיבה."],
        )
        assert "gmail_inbox" in spoken
        assert "אין מיילים" in spoken
    finally:
        db.close()


def test_sheets_aliases_prefetch_locked_contacts_and_activity() -> None:
    from app.domain.two_state import is_sheets_health_ask
    from app.surfaces.turn_coalesce import detect_asked_toolkit

    assert is_sheets_health_ask("Sheets עדיין עובד?")
    assert is_sheets_health_ask("גוגל שיטס")
    assert is_sheets_health_ask("האקסל")
    assert detect_asked_toolkit("Sheets עדיין עובד?") == "CRM"
    init_db()
    db = get_session_factory()()
    sheets = FakeSheetsPort()
    sheets.locked_contacts = [["דנה", "0501234567", "dana@example.com"]]
    try:
        ctx = ToolContext(
            principal=Principal.owner(source="telegram", actor_id="1"),
            store=LeadStore(db),
            brain=BrainStore(db),
            settings=Settings(_env_file=None),
            embedding_port=FakeEmbeddingPort(),
            sheets=sheets,
            owner_text="Sheets עדיין עובד?",
        )
        result = execute_tool("crm_search", {"query": "Sheets עדיין עובד?"}, ctx)
        assert result.ok is True
        assert result.text == "No CRM contact matched."
        assert "lead_" not in result.text.lower()
        assert "docs.google.com" not in result.text
        named = execute_tool("crm_search", {"query": "דנה"}, ctx)
        assert "דנה" in named.text
        assert "lead_" not in named.text.lower()
    finally:
        db.close()
