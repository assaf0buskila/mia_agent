"""Small, deterministic routing helpers for owner meta-requests."""

from __future__ import annotations

import re

_NO_HISTORY_PATTERNS = (
    re.compile(r"(?:אל|בלי)\s+(?:תשתמשי|תשתמש|להשתמש)?\s*(?:ב)?היסטורי(?:ה|ית)", re.I),
    re.compile(r"(?:don't|do not)\s+use\s+(?:the\s+)?history", re.I),
    re.compile(r"(?:without|ignore)\s+(?:the\s+)?history", re.I),
)
_INVENTORY_PATTERNS = (
    re.compile(r"מה\s+(?:הם\s+)?הכלים\s+שלך", re.I),
    re.compile(r"איזה\s+כלים\s+(?:יש\s+)?לך", re.I),
    re.compile(r"(?:תני|תן|הציגי|הצג)\s+(?:לי\s+)?(?:את\s+)?רשימת\s+הכלים(?:\s+שלך)?", re.I),
    re.compile(r"what\s+(?:tools\s+do\s+you\s+have|are\s+your\s+tools)", re.I),
    re.compile(r"list\s+(?:me\s+)?your\s+tools", re.I),
)
_INVENTORY_FILLER = re.compile(
    r"(?:בבקשה|נא|please|mia|מיה|לי|just|רק|עכשיו|currently|current|available)", re.I
)
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


def is_tool_inventory_request(text: str) -> bool:
    """Match a tools-list meta-question, but reject requests containing another action."""
    remainder = text.strip()
    matched = False
    for pattern in _INVENTORY_PATTERNS:
        remainder, count = pattern.subn(" ", remainder, count=1)
        if count:
            matched = True
            break
    if not matched:
        return False
    for pattern in _NO_HISTORY_PATTERNS:
        remainder = pattern.sub(" ", remainder)
    remainder = _INVENTORY_FILLER.sub(" ", remainder)
    remainder = re.sub(r"[\s?!.,:;\-–—'\"׳״]+", "", remainder)
    return not remainder


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


def categorized_tool_names() -> frozenset[str]:
    """Registry coverage seam for a mechanical drift test."""
    return frozenset().union(*(names for _, names in _TOOL_GROUPS))
