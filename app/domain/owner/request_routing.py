"""Small, deterministic routing helpers for owner meta-requests."""

from __future__ import annotations

import re

_NO_HISTORY_PATTERNS = (
    re.compile(r"(?:אל|בלי)\s+(?:תשתמשי|תשתמש|להשתמש)?\s*(?:ב)?היסטורי(?:ה|ית)", re.I),
    re.compile(r"(?:don't|do not)\s+use\s+(?:the\s+)?history", re.I),
    re.compile(r"(?:without|ignore)\s+(?:the\s+)?history", re.I),
)
# Filler is stripped BEFORE matching (see capability_request_kind), so every entry
# here must be \b-bounded: Hebrew has no separate word-break token, and an unbounded
# "כל" or "לי" would silently eat those letters out of the middle of "הכלים" or
# "שלי". Final-form letters (ן/ם/ך/ף/ץ) only ever appear word-final, which already
# gives some of these a natural boundary, but every entry is bounded explicitly here
# so that guarantee never depends on which letter happens to end the word.
#
#   פשוט            -- "just/simply" padding the verb, e.g. "תפרטי לי *פשוט* את..."
#   את               -- the accusative direct-object marker AND the fem. "you"
#                        pronoun (same spelling); both are direction-neutral padding
#                        once the anchored patterns below already require the exact
#                        verb and the exact "tools"/"capabilities" noun.
#   כל               -- "all/every", e.g. "את *כל* היכולות שלך".
#   הכל / הכול       -- "everything" (two spellings), Assaf's own live trailing
#                        "...שלך, הכל".
#   me / everything / all / your -- the English equivalents of the same four.
#   תגידי / תגיד / tell -- the "tell me ..." lead-in, so "תגידי לי מה את יכולה
#                        לעשות" reaches the anchored pattern below instead of
#                        falling through to the model. This one widens a guard that
#                        already worked, so why it is safe is worth stating: the
#                        filler only removes the lead-in, and the remainder must
#                        STILL be a complete capability question and nothing else.
#                        "תגידי לי מה יש ביומן" strips to "מה יש ביומן", which
#                        matches no pattern; "תגידי לי מה הכלים שלך ותשלחי מייל"
#                        leaves "ותשלחי מייל" in the remainder and is rejected.
#                        Both are pinned by tests.
# Each addition has a paired negative test in test_owner_capability_routing.py
# proving it does not swallow an ordinary business request containing that word.
_INVENTORY_FILLER = re.compile(
    r"(?:"
    r"\bבבקשה\b|\bנא\b|\bplease\b|\bmia\b|\bמיה\b|\bלי\b|\bjust\b|\bרק\b|\bעכשיו\b|"
    r"\bcurrently\b|\bcurrent\b|\bavailable\b|"
    r"\bפשוט\b|\bאת\b|\bכל\b|\bהכל\b|\bהכול\b|"
    r"\bme\b|\beverything\b|\ball\b|\byour\b|"
    r"\bתגידי\b|\bתגיד\b|\btell\b"
    r")",
    re.I,
)
# "Tools" is the existing narrow inventory question ("what tools do you have").
# Patterns are matched against the text AFTER _INVENTORY_FILLER has already been
# stripped, so the optional "לי"/"את"/"your"/"me" groups the old patterns needed are
# gone -- the filler strip now does that job once, for every pattern below.
_TOOLS_PATTERNS = (
    re.compile(r"\bמה\s+(?:הם\s+)?הכלים\s+שלך\b", re.I),
    re.compile(r"\bאיזה\s+כלים\s+(?:יש\s+)?לך\b", re.I),
    re.compile(r"\b(?:תני|תן|הציגי|הצג)\s+רשימת\s+הכלים(?:\s+שלך)?\b", re.I),
    re.compile(r"\bwhat\s+(?:tools\s+do\s+you\s+have|are\s+tools)\b|\blist\s+tools\b", re.I),
)
# "Capabilities" is the broader "what can you do" meta-question. Same anti-false-
# positive rule applies: the whole remainder (after filler strip and a match here)
# must be empty, or the text falls through to the model as an ordinary request.
_CAPABILITY_PATTERNS = (
    re.compile(
        r"\bמה\s+(?:היכולות\s+שלך|יכ(?:ול|ולה)\s+לעשות|אפשר\s+לעשות\s+איתך)\b", re.I
    ),
    re.compile(r"\b(?:תפרטי|תני|תן|הציגי|הצג)\s+היכולות\s+שלך\b", re.I),
    # "you can do" alongside "can you do": the "tell me what ..." lead-in inverts the
    # word order, and the sibling `show ... you can do` pattern below already relies on
    # the same inverted form. The empty-remainder rule still applies, so "what you can
    # do for this client" keeps "for this client" and is rejected.
    re.compile(
        r"\bwhat\s+(?:can\s+you\s+do|you\s+can\s+do|are\s+capabilities)\b", re.I
    ),
    re.compile(r"\bshow\s+you\s+can\s+do\b", re.I),
)
_KIND_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    ("tools", _TOOLS_PATTERNS),
    ("capabilities", _CAPABILITY_PATTERNS),
)
_TRAILING_NOISE = re.compile(r"[\s?!.,:;\-–—'\"׳״]+")
_PENDING_APPROVAL_PATTERNS = (
    re.compile(r"מה\s+מחכה\s+לאישור", re.I),
    re.compile(r"(?:what|which)\s+(?:is\s+)?(?:waiting|pending).*approval", re.I),
)


def is_pending_approvals_request(text: str) -> bool:
    return any(pattern.search(text.strip()) for pattern in _PENDING_APPROVAL_PATTERNS)

_TOOL_GROUPS: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "זיכרון וידע",
        frozenset(
            {
                "search_memory",
                "search_knowledge",
                "refresh_website_knowledge",
                "remember",
                "list_known_entities",
            }
        ),
    ),
    (
        "תפעול ולידים",
        frozenset(
            {
                "daily_brief",
                "weekly_brief",
                "hot_leads",
                "pending_approvals",
                "owner_uncertain_writes",
                "website_conversations",
                "operator_snapshot",
                "owner_status",
                "owner_system_audit",
                "lead_review",
                "find_leads",
                "meeting_brief",
                "booked_meetings",
                "content_ideas",
            }
        ),
    ),
    (
        "יומן ומייל",
        frozenset(
            {
                "calendar_availability",
                "calendar_agenda",
                "calendar_create_meeting",
                "calendar_reschedule",
                "gmail_summary",
                "gmail_inbox",
                "gmail_search",
                "gmail_brief",
                "gmail_read",
                "gmail_create_draft",
            }
        ),
    ),
    (
        "CRM ו-Sheets",
        frozenset(
            {
                "crm_search",
                "crm_upsert",
                "crm_record_activity",
                "crm_conflicts",
                "crm_resolve_conflict",
                "sheets_list_tabs",
                "sheets_read",
                "sheets_update",
                "sheets_append",
            }
        ),
    ),
    (
        "אתר, SEO ורשתות",
        frozenset(
            {
                "seo_snapshot",
                "website_kpis",
                "linkedin_snapshot",
                "instagram_insights",
                "social_capabilities",
            }
        ),
    ),
    ("מחקר ציבורי", frozenset({"research_search"})),
    (
        "כלים מחיבורים פעילים נוספים",
        frozenset(
            {
                "composio_search_tools",
                "composio_get_tool_schema",
                "composio_execute_tool",
                "composio_propose_linkedin_action",
                "composio_propose_action",
            }
        ),
    ),
)


def requests_no_history(text: str) -> bool:
    """Whether the owner explicitly asked this turn not to use conversation history."""
    return any(pattern.search(text) for pattern in _NO_HISTORY_PATTERNS)


def capability_request_kind(text: str) -> str:
    """"" (not a capability/meta request) | "tools" | "capabilities".

    Order: strip filler first, then try each anchored pattern, then require the
    remainder to be empty. That last rule is the load-bearing anti-false-positive
    mechanism (carried over unchanged from the old is_tool_inventory_request): a
    phrase only counts once filler, the matched anchor, and any explicit no-history
    clause are all removed and *nothing else is left* -- "...ותבדקי לי את המייל"
    left over after the tools anchor matches means this is an ordinary business
    request, not a meta-question, and it falls through to "" (the model path).
    """
    remainder = text.strip()
    if not remainder:
        return ""
    remainder = _INVENTORY_FILLER.sub(" ", remainder)
    for kind, patterns in _KIND_PATTERNS:
        for pattern in patterns:
            candidate, count = pattern.subn(" ", remainder, count=1)
            if not count:
                continue
            for no_history in _NO_HISTORY_PATTERNS:
                candidate = no_history.sub(" ", candidate)
            candidate = _TRAILING_NOISE.sub("", candidate)
            if not candidate:
                return kind
    return ""


def is_tool_inventory_request(text: str) -> bool:
    """Match a tools-list meta-question, but reject requests containing another action."""
    return capability_request_kind(text) == "tools"


def owner_tool_inventory_reply() -> str:
    """Describe the current owner registry without calling a model or a provider."""
    # Lazy import keeps requests_no_history usable by the owner brain without making
    # request routing depend on the full tool graph during module import.
    from app.tools.registries.owner_tools import get_tool, tool_names

    registered = tool_names()
    # A missing description is a registry contract bug. Do not silently advertise it.
    described = {name for name in registered if (get_tool(name) and get_tool(name).description)}
    groups = [
        (label, [name for name in registered if name in names and name in described])
        for label, names in _TOOL_GROUPS
    ]
    groups = [(label, names) for label, names in groups if names]
    categorized = set().union(*(names for _, names in _TOOL_GROUPS))
    uncategorized = [name for name in registered if name not in categorized]
    if uncategorized:
        groups.append(("כלים רשומים נוספים", uncategorized))

    lines = [
        f"יש לי כרגע {len(registered)} כלים רשומים:",
        *[f"• {label}: {', '.join(names)}" for label, names in groups],
        "",
        (
            "יש בהם קריאות מידע, זיכרון וכתיבות מקומיות או מוגבלות. "
            "שליחה, פרסום ויצירת פגישה נעשים רק במסלול המוגן ובאישור כשנדרש."
        ),
        (
            "זו רשימת היכולות הרשומות כרגע, לא בדיקת חיבור חיה. "
            "אם תרצה, אגיד מה עושה כלי מסוים או נבדוק חיבור מסוים בנפרד."
        ),
    ]
    return "\n".join(lines)


# One short, business-facing Hebrew sentence per _TOOL_GROUPS category: what it does
# *for the business*, not which Python tool names back it. Never describe an ability
# no registered tool actually provides -- if a category gets a new tool, extend its
# sentence to match what that tool does, don't leave a stale blurb making a bigger
# promise than the registry backs.
_CAPABILITY_BLURBS: dict[str, str] = {
    "זיכרון וידע": "לזכור מה שסיכמתם ולחפש בזיכרון ובידע הפומבי שהאתר מפרסם",
    "תפעול ולידים": "סיכומי יום/שבוע, מי ממתין לאישור, לידים חמים ושיחות אתר",
    "יומן ומייל": "לבדוק יומן ומייל, ולהכין טיוטת מייל או הצעת פגישה לאישור",
    "CRM ו-Sheets": "לחפש ולעדכן אנשי קשר בגיליון ה-CRM הנעול",
    "אתר, SEO ורשתות": "נתוני SEO ותנועה לאתר, לינקדאין ואינסטגרם",
    "מחקר ציבורי": "חיפוש מידע פומבי ברשת",
    "כלים מחיבורים פעילים נוספים": "כלים נוספים מחיבורים שמחוברים בפועל, כשצריך",
}


def owner_capability_reply() -> str:
    """Business-friendly capability answer built from the live owner registry.

    Same contract as owner_tool_inventory_reply -- a pure function of the registry,
    no model, provider, Composio adapter, health check or DB access -- grouped into
    business-friendly Hebrew categories instead of raw tool names. Never invents an
    ability outside the registry: a tool missing a description is excluded from its
    count exactly as owner_tool_inventory_reply excludes it, rather than silently
    advertised.
    """
    # Lazy import for the same reason as owner_tool_inventory_reply: keep request
    # routing free of the full tool graph at module import time.
    from app.tools.registries.owner_tools import get_tool, tool_names

    registered = tool_names()
    described = {name for name in registered if (get_tool(name) and get_tool(name).description)}
    groups = [
        (label, [name for name in registered if name in names and name in described])
        for label, names in _TOOL_GROUPS
    ]
    groups = [(label, names) for label, names in groups if names]

    lines = [
        f"אלה היכולות הרשומות שלי כרגע, {len(registered)} כלים בסך הכול:",
        *[
            f"• {label} ({len(names)}): {_CAPABILITY_BLURBS.get(label, '')}"
            for label, names in groups
        ],
        "",
        (
            "יש בהן קריאות מידע, זיכרון וכתיבות מקומיות או מוגבלות. "
            "שליחה, פרסום ויצירת פגישה נעשים רק במסלול המוגן ובאישור כשנדרש."
        ),
        (
            "זו רשימת היכולות הרשומות כרגע, לא בדיקת חיבור חיה. "
            "אם תרצה, אני יכולה גם לשלוח את רשימת שמות הכלים המדויקים."
        ),
    ]
    return "\n".join(lines)


def categorized_tool_names() -> frozenset[str]:
    """Registry coverage seam for a mechanical drift test."""
    return frozenset().union(*(names for _, names in _TOOL_GROUPS))
