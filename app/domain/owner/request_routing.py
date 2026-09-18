"""Small, deterministic routing helpers for owner meta-requests."""

from __future__ import annotations

import re

_NO_HISTORY_PATTERNS = (
    re.compile(r"(?:אל|בלי)\s+(?:תשתמשי|תשתמש|להשתמש)?\s*(?:ב)?היסטורי(?:ה|ית)", re.I),
    re.compile(r"(?:don't|do not)\s+use\s+(?:the\s+)?history", re.I),
    re.compile(r"(?:without|ignore)\s+(?:the\s+)?history", re.I),
)
# Ingress normalisation, applied before anything else. Bidi and zero-width controls
# carry no meaning for intent matching, and the empty-remainder rule in
# `capability_request_kind` is maximally sensitive to any character it does not
# recognise: one stray U+200F leaves a non-empty remainder and sends even Assaf's
# exact sentence back to the slow model path.
#
# This is not hypothetical, and the precise reason took two corrections to get
# right. Mia's OWN egress inserts exactly these characters:
# `app/integrations/telegram_format.py`'s `owner_text()` adds a U+200F where a line
# needs it and `isolate()` wraps LTR runs in U+2068/U+2069. It is a no-op on
# pure-Hebrew and pure-Latin text -- but a Hebrew capability question carrying an
# English filler word ("מה הכלים שלך please", "tell me מה הכלים שלך") both routes
# AND is mixed-script, so egress does mark it up, one case gaining a U+200F. So the
# round trip is genuinely reachable: Mia sends a line, Assaf copies part of it back,
# and without this the invisible marks push it to the model path -- a failure
# invisible in Telegram and indistinguishable from "the fix never shipped".
# Measured in `test_mias_own_egress_really_does_feed_marks_back_into_this_route`.
#
# Deliberately written with real \u escapes rather than a raw string, so the
# characters are resolved at parse time and never depend on regex-level escape
# handling. This repo has already shipped a `\b` that became a literal 0x08 inside a
# pattern that compiled fine and matched nothing.
#
# Stripping these can only ever SHRINK the remainder, so it cannot create a match
# that the anchored patterns plus the empty-remainder rule would not already accept.
_BIDI_AND_ZERO_WIDTH = re.compile(
    "["
    "\\u200b\\u200c\\u200d"  # ZWSP, ZWNJ, ZWJ
    "\\u200e\\u200f"  # LRM, RLM
    "\\u202a\\u202b\\u202c\\u202d\\u202e"  # LRE, RLE, PDF, LRO, RLO
    "\\u2066\\u2067\\u2068\\u2069"  # LRI, RLI, FSI, PDI
    "\\ufeff"  # BOM / ZWNBSP
    "]"
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
    r"\bתגידי\b|\bתגיד\b|\btell\b|"
    #   עוד / else     -- "what *else* can you do", "מה *עוד* את יכולה לעשות".
    #                     Safe: "מה עוד יש ביומן" strips to "מה יש ביומן", which
    #                     matches nothing.
    #   הזמינים/הזמינות -- "available", the Hebrew of a filler already in this set.
    r"\bעוד\b|\belse\b|\bהזמינים\b|\bהזמינות\b"
    r")",
    re.I,
)
# Multi-word lead-ins, stripped like filler but too long to express as word
# alternatives. Same bounded logic: removing the lead-in cannot create a match on its
# own, because whatever follows must STILL be a complete meta-question and nothing
# else. "אני רוצה לדעת מה יש ביומן" strips to "מה יש ביומן" and matches nothing.
_LEAD_INS = re.compile(
    r"(?:"
    r"אני\s+רוצה\s+לדעת|"
    r"i\s+want\s+to\s+know|"
    r"let\s+me\s+know|"
    r"i'?d\s+like\s+to\s+know"
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
    # "רשימת" is optional and the imperative set includes תפרטי/תפרט: "הצג את כל
    # הכלים שלך" and "תפרטי את הכלים שלך" are the same request as "תן רשימת הכלים"
    # and were falling through to the model.
    re.compile(
        r"\b(?:תני|תן|הציגי|הצג|תפרטי|תפרט)\s+(?:רשימת\s+)?הכלים(?:\s+שלך)?\b", re.I
    ),
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
    # "איזה יכולות יש לך" -- the sibling of the tools-side "איזה כלים יש לך", which
    # already existed. Asking the same question about capabilities instead of tools
    # took the slow path.
    re.compile(r"\bאיזה\s+יכולות\s+(?:יש\s+)?לך\b", re.I),
    # Bare noun phrases, reached once a lead-in and filler are stripped: "תגידי לי
    # את היכולות שלך", "tell me your capabilities", "list your capabilities",
    # "אני רוצה לדעת מה היכולות שלך". Safe for the same reason every other pattern
    # here is: the remainder must still be empty afterwards, so "תעדכני את היכולות
    # שלך במסמך" keeps "תעדכני במסמך" and is rejected, as is "your capabilities are
    # limited" ("are limited").
    re.compile(r"\bהיכולות\s+שלך\b", re.I),
    re.compile(r"\bcapabilities\b", re.I),
    # The twin of the tools-side `list tools`, which already existed. Tried after the
    # bare `capabilities` pattern above, which matches but leaves "list" behind -- the
    # loop keeps going on a non-empty remainder, so this still gets its chance.
    re.compile(r"\blist\s+capabilities\b", re.I),
    re.compile(r"\bwhat\s+are\s+you\s+capable\s+of\b", re.I),
    # "you can do" alongside "can you do": the "tell me what ..." lead-in inverts the
    # word order, and the sibling `show ... you can do` pattern below already relies on
    # the same inverted form. The empty-remainder rule still applies, so "what you can
    # do for this client" keeps "for this client" and is rejected.
    re.compile(
        r"\bwhat\s+(?:can\s+you\s+do|you\s+can\s+do|are\s+capabilities)\b", re.I
    ),
    # The optional "what" matters: "show me what you can do" is the more natural of
    # the two phrasings, and without it the earlier `what you can do` pattern matches
    # first, leaves "show" in the remainder, and the phrase is rejected -- while
    # "show me everything you can do" was accepted. Same question, opposite answer.
    re.compile(r"\bshow\s+(?:what\s+)?you\s+can\s+do\b", re.I),
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
    remainder = _BIDI_AND_ZERO_WIDTH.sub("", text).strip()
    if not remainder:
        return ""
    remainder = _LEAD_INS.sub(" ", remainder)
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
# Catch-all for a registered tool that no _TOOL_GROUPS entry claims. Mirrors
# owner_tool_inventory_reply's own "כלים רשומים נוספים" group. Its blurb is
# deliberately vague because, by definition, nothing here knows what the tool does.
_UNCATEGORIZED_LABEL = "יכולות רשומות נוספות"

_CAPABILITY_BLURBS: dict[str, str] = {
    _UNCATEGORIZED_LABEL: "כלים רשומים שעדיין לא מקוטלגים בקטגוריה עסקית",
    "זיכרון וידע": "לזכור מה שסיכמתם ולחפש בזיכרון ובידע הפומבי שהאתר מפרסם",
    "תפעול ולידים": "סיכומי יום/שבוע, מי ממתין לאישור, לידים חמים ושיחות אתר",
    "יומן ומייל": "לבדוק יומן ומייל, ולהכין טיוטת מייל או הצעת פגישה לאישור",
    "CRM ו-Sheets": "לחפש ולעדכן אנשי קשר בגיליון ה-CRM הנעול",
    "אתר, SEO ורשתות": "נתוני SEO ותנועה לאתר, לינקדאין ואינסטגרם",
    "מחקר ציבורי": "חיפוש מידע פומבי ברשת",
    # "שהוגדרו" (configured), never "שמחוברים בפועל" (actually connected): these five
    # Composio tools are registered unconditionally and this function runs no health
    # check, so claiming a live connection here would contradict the reply's own
    # closing line ("לא בדיקת חיבור חיה") two lines later.
    "כלים מחיבורים פעילים נוספים": "כלים נוספים מחיבורים שהוגדרו, אם החיבור פעיל",
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
    # Parity with owner_tool_inventory_reply, which appends its own catch-all group.
    # Without this, registry drift would make the stated total disagree with the sum
    # of the category counts -- two drift tests do fail CI on that, so this is about
    # the answer staying coherent if it ever ships anyway, not about the guard.
    categorized = set().union(*(names for _, names in _TOOL_GROUPS))
    uncategorized = [name for name in registered if name not in categorized and name in described]
    if uncategorized:
        groups.append((_UNCATEGORIZED_LABEL, uncategorized))

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
