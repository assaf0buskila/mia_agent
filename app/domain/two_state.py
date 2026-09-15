"""Two-state Mia tools: Owner Telegram vs visitor site.

Owner = Dude, full house Composio, never sell to Assaf.
Visitor = seller, few tools, identity before ping — not before product answers.
"""

from __future__ import annotations

import re
from enum import StrEnum
from functools import cache

from app.capabilities.types import Principal

STILL_CHECKING = "still checking"
# Every house adapter allows itself 20s (SEO 25s, Apify 70s). A 12s budget meant any
# tool needing 12-20s ALWAYS reported "still checking" while its real answer was
# computed and then discarded — the owner could never get it. Sit just above the 20s
# adapter ceiling so a normal slow call still returns a real result, and keep the slow
# budget under the 45s `asyncio.wait_for` around the whole turn in
# `app.workers.telegram_owner`, otherwise the outer guard fires first and the extra
# patience buys nothing.
TOOL_TIMEOUT_SECONDS = 22
TOOL_RECOVERY_SECONDS = 16
SLOW_HOUSE_TOOLS = frozenset(
    {
        "crm_search",
        "crm_upsert",
        "sheets_read",
        "sheets_list_tabs",
        "instagram_insights",
        # Multi-provider reads: each fans out to two or more 20s adapters, so they
        # never fitted in the base budget.
        "seo_snapshot",
        "website_kpis",
        "research_search",
        "owner_system_audit",
    }
)

OWNER_HOUSE_TOOLS: frozenset[str] = frozenset(
    {
        "search_memory",
        "search_knowledge",
        "refresh_website_knowledge",
        "remember",
        "list_known_entities",
        "daily_brief",
        "weekly_brief",
        "hot_leads",
        "pending_approvals",
        "owner_uncertain_writes",
        "website_conversations",
        "operator_snapshot",
        "owner_status",
        "sheets_list_tabs",
        "owner_system_audit",
        "lead_review",
        "find_leads",
        "meeting_brief",
        "calendar_availability",
        "calendar_agenda",
        "calendar_create_meeting",
        "calendar_reschedule",
        "booked_meetings",
        "content_ideas",
        "gmail_summary",
        "gmail_inbox",
        "gmail_search",
        "gmail_brief",
        "gmail_read",
        "gmail_create_draft",
        "seo_snapshot",
        "website_kpis",
        "linkedin_snapshot",
        "social_capabilities",
        "crm_search",
        "crm_upsert",
        "crm_record_activity",
        "crm_conflicts",
        "crm_resolve_conflict",
        "sheets_read",
        "sheets_update",
        "sheets_append",
        "instagram_insights",
        "research_search",
        "composio_search_tools",
        "composio_get_tool_schema",
        "composio_execute_tool",
        "composio_propose_linkedin_action",
        "composio_propose_action",
    }
)

VISITOR_TOOLS: frozenset[str] = frozenset(
    {
        "search_knowledge",
    }
)

FORBIDDEN_OWNER_TOOLS: frozenset[str] = frozenset(
    {
        "gmail_send",
        "instagram_publish",
        "linkedin_post",
        "whatsapp_send_lead",
    }
)

_IDENTITY_ACTIONS: frozenset[str] = frozenset(
    {
        "ping",
        "crm_write",
        "whatsapp_offer",
        "handoff",
    }
)

_SHEETS_NEEDLES: tuple[str, ...] = (
    "google sheets",
    "google sheet",
    "גוגל שיטס",
    "גוגל שיט",
    "האקסל",
    "אקסל",
    "excel",
    "contacts",
    "crm",
    "שיטס",
    "שיט",
    "sheets",
    "sheet",
)

_TOOLKIT_NEEDLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "instagram",
        ("instagram", "אינסטגרם", "אינסטה", "ig ", " ריל", "reel", "reels", "פוסט"),
    ),
    ("gmail", ("gmail", "מייל", "inbox", "דואר")),
    ("calendar", ("יומן", "calendar", "פגישה", "agenda")),
    ("gsc", ("search console", "gsc", "קונסולת חיפוש", "impressions")),
    ("ga4", ("ga4", "analytics", "אנליטיקס", "traffic", "תנועה")),
    ("sheets", _SHEETS_NEEDLES),
    ("linkedin", ("linkedin", "לינקדאין", "לינקדין", "לינקד אין")),
    ("whatsapp", ("whatsapp", "וואטסאפ", "ווטסאפ")),
)

# Instagram and LinkedIn are the only two toolkits that share a generic trigger word
# ("פוסט"/"post" — see the instagram entry above). Because `_TOOLKIT_NEEDLES` is scanned
# in order and instagram comes first, a sentence that names LinkedIn explicitly while
# also containing that generic word ("תכתבי לי פוסט ללינקדאין") would otherwise resolve
# to instagram just by being scanned first. These two lists exist ONLY to break that
# specific tie — no other toolkit has this collision, so no other toolkit needs one.
#
# Bare Latin "insta" and "linked in" are deliberately NOT registered anywhere, even
# though they are real colloquial spellings: "insta" is a substring of ordinary English
# words ("install", "instant", "instance", "instability") and would hijack them, and
# "linked in" reads as an ordinary two-word phrase ("I linked in the doc") far more
# often than as the platform name — a word-boundary check cannot tell those apart since
# "in" already ends on a natural boundary either way. The Hebrew spellings (אינסטה,
# לינקדין, לינקד אין) have no such collision risk and stay.
_INSTAGRAM_EXPLICIT_NEEDLES: tuple[str, ...] = ("instagram", "אינסטגרם", "אינסטה")
_LINKEDIN_EXPLICIT_NEEDLES: tuple[str, ...] = (
    "linkedin",
    "לינקדאין",
    "לינקדין",
    "לינקד אין",
)

# "crm" alone names the topic ("a LinkedIn post about crm") at least as often as it
# names the Contacts/Activity sheet -- unlike every other sheets needle ("sheet",
# "excel", "contacts", "google sheets", ...), which never means anything else. This
# is the one sheets needle weak enough to yield to an explicitly named platform; see
# its use in `asked_toolkit` below.
_SHEETS_WEAK_NEEDLES = frozenset({"crm"})

# Gates the crm/linkedin override above: a bare "crm" plus an explicit LinkedIn
# mention is at least as often a CRM *write* that merely mentions LinkedIn as
# context ("add the linkedin lead to crm") as it is a content topic. Requiring one
# of these content words keeps the override scoped to what it was built for.
_SOCIAL_CONTENT_WORDS: tuple[str, ...] = ("post", "פוסט", "comment", "caption")

# Whole-word matching (below) is stricter than the old substring check, so it can
# silently lose an inflected form the old check caught only by accident (e.g.
# "reel" used to match "reels" as a substring). Audited every ASCII needle above
# for this when whole-word matching was introduced:
#   - "reel" -> added "reels" to the instagram tuple: "how are my reels doing" is
#     the single commonest Instagram-performance question.
#   - "excel" -> deliberately NOT widened to match "excels": that is the ordinary
#     verb ("she excels at her job"), not the spreadsheet -- widening it would
#     reintroduce the exact "excellent" false-positive class this fix removed.
#   - "contacts" -> deliberately NOT widened to also match bare "contact": that
#     word is far too generic ("let's contact him") and was never caught by the
#     old substring check either (the needle is longer than the singular form),
#     so this is a pre-existing scope choice, not a regression.
#   - "inbox", "calendar", "agenda" -> plural forms ("inboxes", "calendars",
#     "agendas") are vanishingly rare for Assaf's single Gmail inbox and single
#     calendar; left narrow rather than adding needles nothing will ever use.
#   - "sheet"/"sheets", "google sheet"/"google sheets", "content idea"/
#     "content ideas" -> already registered as separate singular/plural needles,
#     so neither form was ever at risk.
#   - "impressions", "analytics", "traffic" -> already the only natural form used
#     in this context (mass nouns, or a metric name that is not used in the
#     singular); the old substring check never caught a shorter singular either,
#     since the needle is longer than it, so there is nothing to lose here.
#   - A possessive ("LinkedIn's", "Instagram's") is unaffected either way: `'` is
#     not a word character, so it already reads as a boundary on its own.


@cache
def _latin_word_pattern(needle: str) -> re.Pattern[str]:
    # A boundary defined only against a Latin/Hebrew *letter* on either side --
    # not Python's `\w` (letters + digits + underscore). `\w` made
    # "instagram_insights" and "gmail_brief" fail to match their own toolkit name
    # (no transition between two `\w` characters at the underscore) and made a
    # spreadsheet tab name like "Sheet2" fail the same way (no transition between
    # a letter and a digit). `[^\W\d_]` is "a `\w` character that is not a digit
    # and not `_`", i.e. a letter -- so both a digit and an underscore now read as
    # a boundary, while an adjacent *letter* ("excellent", "spreadsheet") still
    # correctly blocks the match.
    esc = re.escape(needle.strip())
    return re.compile(rf"(?<![^\W\d_]){esc}(?![^\W\d_])")


def _needle_hit(needle: str, *, blob: str, text: str) -> bool:
    """Whole-word match for a pure-ASCII needle; substring match otherwise.

    A loose substring match let "excel" (sheets) fire inside "excellent" and "ig "
    (instagram, meant as the standalone abbreviation) fire inside "big " or
    "config " -- the trailing space was meant as a boundary but a substring check
    still finds it as a suffix of a larger word. The letter-only boundary above
    fixes every one of those without touching intentional matches, an inflected
    form the old substring check happened to catch (see the audit above), or a
    needle glued to a digit or underscore ("Sheet2", "gmail_brief").
    Hebrew needles keep the old substring behaviour: the same boundary treats
    Hebrew letters as letters too, so it cannot separate one Hebrew word glued to
    another ("שיט" inside "שיטה") any better than a plain substring check does --
    the sheets/linkedin tie-break in `asked_toolkit` is what actually guards that
    one Hebrew collision on record, not this function.
    """
    if needle.strip().isascii():
        pattern = _latin_word_pattern(needle)
        return pattern.search(blob) is not None or pattern.search(text) is not None
    return needle in blob or needle in text


class MiaState(StrEnum):
    OWNER = "owner"
    VISITOR = "visitor"


def state_for(principal: Principal) -> MiaState:
    """Visitor vs owner follows the Principal minted at the channel entry."""
    if principal.graph == "owner":
        return MiaState.OWNER
    return MiaState.VISITOR


def tools_for(state: MiaState) -> frozenset[str]:
    if state is MiaState.OWNER:
        return OWNER_HOUSE_TOOLS
    return VISITOR_TOOLS


def may_run(*, state: MiaState, tool: str) -> bool:
    name = tool.strip()
    if name in FORBIDDEN_OWNER_TOOLS:
        return False
    if state is MiaState.VISITOR:
        return name in VISITOR_TOOLS
    return True


def identity_required_for(action: str) -> bool:
    """Identity is required before ping/CRM/WhatsApp, not before product answers."""
    return action.strip() in _IDENTITY_ACTIONS


def asked_toolkit(text: str) -> str:
    """The toolkit he named. Empty if he did not name one.

    Runs the plain ordered scan first — unchanged from before, so every toolkit that
    has no shared generic word (gmail, calendar, sheets, ...) keeps its original
    first-match behaviour exactly. Only when that scan's answer is "instagram" do we
    check whether the sentence actually named a platform explicitly: an explicit
    LinkedIn mention (with no explicit Instagram mention) overrides the generic-word
    match, and naming both explicitly forces neither — guessing would be worse than
    asking. A generic word alone ("פוסט" with no explicit name) still resolves to
    instagram, exactly as before.

    The same kind of override applies when the scan's answer is "sheets" but the
    only reason it matched is the weak "crm" needle (`_SHEETS_WEAK_NEEDLES`), an
    explicit LinkedIn is named, AND a content word (`_SOCIAL_CONTENT_WORDS`,
    e.g. "post") is also present: "LinkedIn post about crm" is about the topic,
    not the Contacts sheet. The content-word requirement matters because "crm" +
    an explicit LinkedIn without one is normally a CRM *write* mentioning
    LinkedIn as context ("add the linkedin lead to crm", "update crm after the
    linkedin call") — that must still go to sheets, not be told to answer
    LinkedIn first. A stronger sheets needle in the same sentence ("cheat sheet
    for a linkedin post") still wins normally regardless — only a bare "crm"
    match ever yields.
    """
    blob = f" {text.strip().lower()} "

    def _matches(needles: tuple[str, ...]) -> bool:
        return any(_needle_hit(needle, blob=blob, text=text) for needle in needles)

    candidate = ""
    for toolkit, needles in _TOOLKIT_NEEDLES:
        if _matches(needles):
            candidate = toolkit
            break

    if candidate == "instagram":
        instagram_named = _matches(_INSTAGRAM_EXPLICIT_NEEDLES)
        linkedin_named = _matches(_LINKEDIN_EXPLICIT_NEEDLES)
        if linkedin_named and instagram_named:
            return ""
        if linkedin_named:
            return "linkedin"

    if candidate == "sheets":
        matched_sheets = frozenset(
            needle for needle in _SHEETS_NEEDLES if _needle_hit(needle, blob=blob, text=text)
        )
        if (
            matched_sheets
            and matched_sheets <= _SHEETS_WEAK_NEEDLES
            and _matches(_LINKEDIN_EXPLICIT_NEEDLES)
            and _matches(_SOCIAL_CONTENT_WORDS)
        ):
            return "linkedin"

    return candidate


_CONTENT_IDEA_NEEDLES: tuple[str, ...] = (
    "content idea",
    "content ideas",
    "content plan",
    "what to post",
    "post ideas",
    "רעיון לתוכן",
    "רעיונות לתוכן",
    "רעיונות לפוסט",
    "מה לפרסם",
    "מה כדאי לפרסם",
)


def is_social_writing_turn(text: str) -> bool:
    """LinkedIn, Instagram, or content-ideas turn.

    These are the turns where a model can slide from an observed fact into an
    invented metric or a written draft into a claimed publish; `build_messages`
    injects the social-writing rule only here, never on an unrelated turn.
    """
    if asked_toolkit(text) in {"linkedin", "instagram"}:
        return True
    blob = f" {text.strip().lower()} "
    return any(_needle_hit(needle, blob=blob, text=text) for needle in _CONTENT_IDEA_NEEDLES)


def is_sheets_alias(text: str) -> bool:
    """sheets, Google sheets, גוגל שיטס, האקסל, Contacts, CRM are the locked CRM."""
    return asked_toolkit(text) == "sheets"


def is_sheets_health_ask(text: str) -> bool:
    """First ask about whether the locked Sheet works, not a named contact lookup."""
    stripped = text.strip().strip("?!.").strip()
    if not stripped:
        return True
    if not is_sheets_alias(stripped):
        return False
    blob = f"{stripped} {stripped.casefold()}"
    if any(mark in blob for mark in ("עובד", "working", "connected", "מחובר", "עדיין")):
        return True
    return len(stripped.split()) <= 3


def say_tool_before_numbers(tool: str, body: str) -> str:
    """Numbers are never naked. The tool name comes first."""
    label = tool.strip() or "tool"
    content = body.strip()
    if not content:
        return f"{label}: missing."
    if content.lower().startswith(label.lower()):
        return content
    return f"{label}: {content}"
