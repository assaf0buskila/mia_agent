"""Two-state Mia tools: Owner Telegram vs visitor site.

Owner = Dude, full house Composio, never sell to Assaf.
Visitor = seller, few tools, identity before ping — not before product answers.
"""

from __future__ import annotations

from enum import StrEnum

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
        "remember",
        "list_known_entities",
        "daily_brief",
        "weekly_brief",
        "hot_leads",
        "pending_approvals",
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
        "booked_meetings",
        "content_ideas",
        "gmail_summary",
        "gmail_inbox",
        "gmail_search",
        "gmail_read",
        "gmail_create_draft",
        "seo_snapshot",
        "website_kpis",
        "linkedin_snapshot",
        "crm_search",
        "crm_upsert",
        "sheets_read",
        "sheets_update",
        "sheets_append",
        "instagram_insights",
        "research_search",
        "whatsapp_draft_assaf",
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
        "published_facts",
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
    ("instagram", ("instagram", "אינסטגרם", "ig ", " ריל", "reel", "פוסט")),
    ("gmail", ("gmail", "מייל", "inbox", "דואר")),
    ("calendar", ("יומן", "calendar", "פגישה", "agenda")),
    ("gsc", ("search console", "gsc", "קונסולת חיפוש", "impressions")),
    ("ga4", ("ga4", "analytics", "אנליטיקס", "traffic", "תנועה")),
    ("sheets", _SHEETS_NEEDLES),
    ("linkedin", ("linkedin", "לינקדאין")),
    ("whatsapp", ("whatsapp", "וואטסאפ", "ווטסאפ")),
)


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
    """The toolkit he named. Empty if he did not name one."""
    blob = f" {text.strip().lower()} "
    for toolkit, needles in _TOOLKIT_NEEDLES:
        if any(needle in blob or needle in text for needle in needles):
            return toolkit
    return ""


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
