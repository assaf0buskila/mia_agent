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
    is_social_writing_turn,
    may_run,
    say_tool_before_numbers,
    tools_for,
)
from app.graph.owner_agent import (
    SYSTEM_PROMPT,
    TOOL_DEADLINE_REPLY,
    AgentStep,
    _looks_silent,
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
    # More linkedin/instagram spellings (P3) - Hebrew only; see
    # test_asked_toolkit_drops_ambiguous_latin_spellings for why the Latin colloquial
    # spellings ("insta", "linked in") are deliberately not registered.
    assert asked_toolkit("תכתבי פוסט ללינקדין") == "linkedin"
    assert asked_toolkit("פוסט ללינקד אין בבקשה") == "linkedin"
    assert asked_toolkit("תעלי לי סטורי לאינסטה") == "instagram"


def test_asked_toolkit_drops_ambiguous_latin_spellings() -> None:
    """Bare "insta" and "linked in" are real spellings but ordinary-English magnets.

    Base a04a6d8 routed all four of these correctly; a prior fix that registered bare
    "insta" as an instagram needle broke every one of them because "insta" is a
    substring of common English words and instagram is scanned first. "insta" and
    "linked in" must stay unregistered (the Hebrew spellings אינסטה/לינקדין/לינקד אין
    have no such collision and are covered elsewhere).
    """
    assert asked_toolkit("install the calendar integration") == "calendar"
    assert asked_toolkit("check my calendar for an instant meeting") == "calendar"
    assert asked_toolkit("open the gmail instance") == "gmail"
    assert asked_toolkit("constant instability in traffic") == "ga4"
    # A two-word English phrase reading as ordinary prose, not the platform name.
    assert asked_toolkit("I linked in the doc, make a פוסט") == "instagram"
    # The bare Latin spellings genuinely used for the platforms now match nothing -
    # an accepted, documented tradeoff for not hijacking the sentences above.
    assert asked_toolkit("post it on insta") == ""
    assert asked_toolkit("write a post for linked in") == ""


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


def test_asked_toolkit_whole_word_latin_needles_avoid_false_positives() -> None:
    """A plain substring match let "excel" (sheets) fire inside "excellent" and

    "ig " (instagram, meant as the standalone abbreviation, with a trailing space
    as an improvised boundary) fire inside "big " or "config " -- the space is
    also the last letter of the previous word. Real word-boundary matching fixes
    every one of these without touching a genuine standalone occurrence.
    """
    assert asked_toolkit("a big meeting tomorrow") == ""
    assert asked_toolkit("update the config file") == ""
    assert asked_toolkit("excellent work") == ""
    # The abbreviation and the word, each on its own, still resolve correctly.
    assert asked_toolkit("check my ig") == "instagram"
    assert asked_toolkit("that was an excel formula") == "sheets"


def test_asked_toolkit_whole_word_matching_keeps_common_inflected_forms() -> None:
    """Whole-word matching is stricter than the old substring check, so it can

    silently lose a plural/inflected form the old check caught only by accident.
    "reel" -> "reels" is the one real regression found by audit (the commonest
    Instagram-performance question); every other ASCII needle was audited and
    either already covers both forms or is deliberately left narrow (see the
    comment above `_SOCIAL_CONTENT_WORDS` in two_state.py).
    """
    assert asked_toolkit("how are my reels doing") == "instagram"
    assert asked_toolkit("post a reel today") == "instagram"
    assert asked_toolkit("check my reel") == "instagram"
    # Deliberately still narrow: "excels" is the ordinary verb, not the
    # spreadsheet, and widening it would reintroduce the "excellent" bug class.
    assert asked_toolkit("she excels at her job") == ""


def test_asked_toolkit_underscore_and_digit_glued_names_still_match() -> None:
    """Python's `\\w` (and therefore the old `\\b`) treats digits and

    underscore as word characters, so a needle glued directly to either one
    (a tool name like "instagram_insights", a tab name like "Sheet2") could
    never satisfy a boundary on that side. The boundary is redefined against a
    letter only, so both now read as a boundary; an adjacent letter
    ("spreadsheet", "excellent") still correctly blocks the match.
    """
    assert asked_toolkit("check instagram_insights") == "instagram"
    assert asked_toolkit("run gmail_brief for today") == "gmail"
    assert asked_toolkit("what's in sheet1") == "sheets"
    assert asked_toolkit("what's in Sheet2") == "sheets"
    assert asked_toolkit("a spreadsheet is not a sheet") == "sheets"


def test_asked_toolkit_linkedin_topic_outranks_weak_crm_needle() -> None:
    """"crm" alone names the topic ("a LinkedIn post about crm") as often as it

    names the Contacts sheet, unlike every other sheets needle. An explicitly
    named LinkedIn wins when "crm" is the only sheets needle that matched AND a
    content word ("post"/"פוסט"/"comment"/"caption") is also present; without one,
    "crm" plus an explicit LinkedIn is a CRM *write* that merely mentions
    LinkedIn as context, and must still go to sheets. A real sheets needle in the
    same sentence still wins normally either way.
    """
    assert asked_toolkit("LinkedIn post about crm") == "linkedin"
    assert asked_toolkit("write a linkedin comment about crm") == "linkedin"
    # The content-word gate (P2-2): a CRM write that only mentions LinkedIn as
    # context, with no content word, must not be told to answer LinkedIn first.
    assert asked_toolkit("add the linkedin lead to crm") == "sheets"
    assert asked_toolkit("update crm after the linkedin call") == "sheets"
    assert asked_toolkit("תעדכני crm אחרי השיחה בלינקדאין") == "sheets"
    assert asked_toolkit("תוסיפי את הליד מלינקדאין ל-crm") == "sheets"
    # Regression guards: a bare "crm", and a real sheets needle even alongside an
    # explicit LinkedIn mention, are unaffected by the override above.
    assert asked_toolkit("CRM") == "sheets"
    assert asked_toolkit("cheat sheet for a linkedin post") == "sheets"
    assert asked_toolkit("update the crm sheet, also a linkedin post") == "sheets"


def test_asked_toolkit_crm_linkedin_content_word_gate_covers_plurals_and_gerund() -> None:
    """F1 regression: the content-word gate (P2-2) was itself narrowed by the

    exact inflection-loss class the P2-1 audit was built to catch, just applied
    to the new `_SOCIAL_CONTENT_WORDS` tuple instead of `_TOOLKIT_NEEDLES`. A
    plural ("posts", "comments", "captions") or the gerund ("posting") must gate
    the override exactly like the singular "post"/"comment"/"caption" already do.
    """
    assert asked_toolkit("linkedin posts about crm") == "linkedin"
    assert asked_toolkit("linkedin comments about crm") == "linkedin"
    assert asked_toolkit("linkedin captions about crm") == "linkedin"
    assert asked_toolkit("posting about crm on linkedin") == "linkedin"


def test_asked_toolkit_ascii_needle_after_a_glued_hebrew_clitic_still_matches() -> None:
    """F2 regression: the letter-only boundary from P3-1 (`[^\\W\\d_]`) still

    counted a Hebrew letter as a blocking "letter", so an ASCII needle glued
    directly to a Hebrew clitic with no space -- the definite article "ה" ("the"),
    or a bare preposition like "ב" ("in/on") -- matched nothing, even though the
    hyphenated spelling of the same thing already worked. The boundary is now
    ASCII-Latin-letter-only, so a Hebrew letter reads as a boundary; every P1/
    P3-1 example (English word-adjacency, digit- and underscore-glued names)
    stays exactly as before since none of those involve a Hebrew letter.
    """
    assert asked_toolkit("תעדכני את הCRM") == "sheets"
    assert asked_toolkit("בinstagram שלי") == "instagram"
    # The already-working hyphenated spelling is unaffected.
    assert asked_toolkit("תעדכני את ה-CRM") == "sheets"
    # P1/P3-1 examples, re-run against the new boundary class.
    assert asked_toolkit("excellent work") == ""
    assert asked_toolkit("she excels at her job") == ""
    assert asked_toolkit("a spreadsheet is not a sheet") == "sheets"
    assert asked_toolkit("a big meeting tomorrow") == ""
    assert asked_toolkit("update the config file") == ""
    assert asked_toolkit("install the calendar integration") == "calendar"
    assert asked_toolkit("check my calendar for an instant meeting") == "calendar"
    assert asked_toolkit("open the gmail instance") == "gmail"
    assert asked_toolkit("constant instability in traffic") == "ga4"
    assert asked_toolkit("check instagram_insights") == "instagram"
    assert asked_toolkit("run gmail_brief for today") == "gmail"
    assert asked_toolkit("what's in Sheet2") == "sheets"


def test_asked_toolkit_hebrew_reels_glued_to_the_definite_article_still_matches() -> None:
    """F3 regression: the Hebrew "reel" needle (" ריל", leading space required)

    only ever matched a space-preceded occurrence, so the definite article "ה"
    ("the") attached directly with no space -- "הרילים"/"הרילס", the form Assaf
    actually types -- matched nothing, while the unprefixed form already worked.
    "רילים"/"רילס" are added as their own needles to cover the glued form; the
    bare root "ריל" is deliberately NOT added, since it is a substring of the
    unrelated real word "גריל" (grill) and would reintroduce the exact
    substring-collision class whole-word matching removed for the ASCII needles.
    """
    assert asked_toolkit("איך הרילים שלי עובדים") == "instagram"
    assert asked_toolkit("הרילס שלי") == "instagram"
    # Already worked before this fix (space-preceded); must keep working.
    assert asked_toolkit("רילים שלי") == "instagram"
    assert asked_toolkit("תעלי לי ריל") == "instagram"
    # Guard: the unrelated real word containing "ריל" as a substring must not
    # collide, with or without the definite article.
    assert asked_toolkit("ארוחת גריל") == ""
    assert asked_toolkit("הגריל מוכן") == ""


def test_is_social_writing_turn_matches_linkedin_instagram_and_content_only() -> None:
    for text in (
        "LinkedIn post about crm",
        "post for LinkedIn",
        "תבדקי את האינסטגרם",
        "give me content ideas",
        "רעיונות לתוכן",
        "what to post this week",
    ):
        assert is_social_writing_turn(text) is True, text
    for text in (
        "מה יש לי היום ביומן?",
        "what's on my calendar today",
        "check gmail",
        "היי",
        "",
    ):
        assert is_social_writing_turn(text) is False, text


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


def test_looks_silent_only_matches_a_bare_greeting() -> None:
    """Only an empty reply or a bare greeting (dressed with trailing punctuation

    or an emoji) counts as silent. `startswith` used to also catch a real answer
    that merely opened with the greeting word.
    """
    bare_greetings = (
        "היי",
        "hey",
        "hey!",
        "פה. מה צריך?",
        "here. what do you need?",
        "  Hey  ",
        "hey 👋",
    )
    for bare in bare_greetings:
        assert _looks_silent(bare) is True, bare
    assert _looks_silent("") is True
    assert _looks_silent("   ") is True


def test_looks_silent_preserves_a_greeting_prefixed_real_answer() -> None:
    assert _looks_silent("היי אסף, יש לך 3 מיילים שדורשים תגובה") is False
    assert _looks_silent("hey, here is what I found: 3 pending approvals") is False


def test_looks_silent_decoration_only_is_not_silent() -> None:
    """No greeting token at all -- pure decoration, however minimal -- is a real

    (if terse) reply, not silence.
    """
    for decoration_only in ("👍", "?", "…", "✅"):
        assert _looks_silent(decoration_only) is False, decoration_only


def test_looks_silent_strips_variation_selectors_skin_tones_and_emoticons() -> None:
    """U+FE0F (emoji presentation), a skin tone modifier, and ASCII emoticon

    punctuation (colon/semicolon/hyphen/parens) around a bare greeting must all
    still read as a bare greeting.
    """
    for decorated_greeting in ("hey ❤️", "היי :)", "hey :-)", "hey 👋🏽"):
        assert _looks_silent(decorated_greeting) is True, decorated_greeting


def test_looks_silent_long_reply_without_a_greeting_is_not_silent() -> None:
    for real_reply in (
        "היי אסף, יש לך 3 מיילים שדורשים תגובה",
        "heyyy there",
    ):
        assert _looks_silent(real_reply) is False, real_reply


def test_looks_silent_bare_shalom_is_silent_but_a_name_after_it_is_not() -> None:
    """"שלום" is a bare greeting like "היי"/"hey"; a name after the greeting

    word is real content, not decoration, so it must stay a real answer.
    """
    assert _looks_silent("שלום!") is True
    assert _looks_silent("שלום") is True
    assert _looks_silent("היי אסף! 👋") is False


def test_looks_silent_bails_out_before_the_regex_on_long_input() -> None:
    """The trailing/leading decoration regexes are anchored but `re.sub` still

    tries every position of a long non-matching string, measured quadratic on
    this input shape (~1s at 16k chars, hung at 1e5). A reply this long is
    never a bare greeting regardless, so the length check must short-circuit
    before either regex runs.
    """
    from time import perf_counter

    started = perf_counter()
    result = _looks_silent("x" * 100_000)
    elapsed = perf_counter() - started
    assert result is False
    assert elapsed < 0.5, elapsed


def test_refuse_seen_and_silent_preserves_a_useful_greeting_prefixed_reply() -> None:
    """The raw-tool-report fallback must never overwrite a real answer just

    because it happens to open with the greeting word.
    """
    spoken = _refuse_seen_and_silent(
        "היי אסף, יש לך 3 מיילים שדורשים תגובה",
        [AgentStep(tool="gmail_inbox", ok=True, detail="ok")],
        ["gmail_inbox: 3 מיילים חדשים"],
    )
    assert spoken == "היי אסף, יש לך 3 מיילים שדורשים תגובה"


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
