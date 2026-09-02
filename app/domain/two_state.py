"""Two-state Mia tools: Owner Telegram vs visitor site.

Owner = Dude, full house Composio, never sell to Assaf.
Visitor = seller, few tools, identity before ping — not before product answers.
"""

from __future__ import annotations

from enum import StrEnum

from app.capabilities.types import Principal

STILL_CHECKING = "still checking"
TOOL_TIMEOUT_SECONDS = 12

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

_TOOLKIT_NEEDLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("instagram", ("instagram", "אינסטגרם", "ig ", " ריל", "reel", "פוסט")),
    ("gmail", ("gmail", "מייל", "inbox", "דואר")),
    ("calendar", ("יומן", "calendar", "פגישה", "agenda")),
    ("gsc", ("search console", "gsc", "קונסולת חיפוש", "impressions")),
    ("ga4", ("ga4", "analytics", "אנליטיקס", "traffic", "תנועה")),
    ("sheets", ("שיט", "sheet", "contacts", "crm")),
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


def say_tool_before_numbers(tool: str, body: str) -> str:
    """Numbers are never naked. The tool name comes first."""
    label = tool.strip() or "tool"
    content = body.strip()
    if not content:
        return f"{label}: missing."
    if content.lower().startswith(label.lower()):
        return content
    return f"{label}: {content}"
