"""Structured Hebrew owner lead cards for Telegram.

Assaf reads these on a phone. Each field is its own labeled line so a lead is
scannable, not a paragraph. HTML only: callers that send these must use
parse_mode=HTML. Prospect text is escaped and sanitised; it is never instructions.
"""

from __future__ import annotations

from app.domain.lead_label import lead_display, sanitize_label
from app.domain.memory import ConversationTurn, counterpart_turns
from app.domain.sales import FitLevel, NextAction, SalesState
from app.integrations.telegram_format import bold, code, esc, join_sections

YES = "כן"
NO = "לא"
LAST_SAID_MAX = 80
DISCOVERY_MAX = 240

NEXT_ACTION_HE: dict[str, str] = {
    NextAction.UNDERSTAND_WORKFLOW.value: "הבנת תהליך",
    NextAction.DEEPEN_PAIN.value: "העמקת כאב",
    NextAction.QUANTIFY.value: "כימות",
    NextAction.REFLECT.value: "שיקוף",
    NextAction.OFFER_HYPOTHESIS.value: "השערת אוטומציה",
    NextAction.QUALIFY.value: "כישור",
    NextAction.OFFER_MEETING.value: "הצעת פגישה",
    NextAction.OFFER_WHATSAPP.value: "הצעת וואטסאפ",
    NextAction.HANDOFF.value: "העברה",
    NextAction.HANDLE_OBJECTION.value: "התנגדות",
    NextAction.DISQUALIFY.value: "פסילה",
    NextAction.STOP.value: "עצירה",
}

_FACT_HE: tuple[tuple[str, str], ...] = (
    ("workflow_known", "יודעים מה העסק עושה"),
    ("manual_step_known", "יש שלב ידני ברור"),
    ("data_source_known", "ידוע מאיפה הנתונים מגיעים"),
    ("impact_confirmed", "כאב מאומת"),
    ("hypothesis_offered", "הוצעה השערה"),
    ("explicit_buying_intent", "יש כוונת קנייה מפורשת"),
    ("meeting_exit_offered", "הוצעה פגישה"),
)


def hebrew_yes_no(value: bool) -> str:
    return YES if value else NO


def hebrew_next_action(action: str) -> str:
    key = (action or "").strip()
    if not key:
        return ""
    return NEXT_ACTION_HE.get(key, key)


def last_message_short(turns: list[ConversationTurn], *, limit: int = LAST_SAID_MAX) -> str:
    """Last prospect line, sanitised. Empty when nothing safe survives."""
    recent = counterpart_turns(turns)
    if not recent:
        return ""
    cleaned = sanitize_label(recent[-1].text)
    if not cleaned:
        return ""
    return cleaned[:limit]


def discovery_summary(sales: SalesState) -> str:
    """Short non-PII snapshot of what discovery already established."""
    parts: list[str] = []
    headline = (sales.headline or "").strip()
    if headline:
        parts.append(headline[:80])
    for attr, label in _FACT_HE:
        if getattr(sales, attr, False):
            parts.append(label)
    pain = int(sales.pain_level)
    if pain > 0:
        parts.append(f"כאב P{pain}")
    if not parts:
        parts.append("עדיין בתחילת discovery")
    return " · ".join(parts)[:DISCOVERY_MAX]


def is_disqualified(sales: SalesState, next_action: str = "") -> bool:
    if sales.fit == FitLevel.POOR:
        return True
    return (next_action or "").strip() == NextAction.DISQUALIFY.value


def _line(label: str, value: str, *, monospace: bool = False) -> str:
    cleaned = str(value).strip()
    if not cleaned:
        return ""
    rendered = code(cleaned) if monospace else esc(cleaned)
    return f"{bold(label)}: {rendered}"


def format_owner_lead_card(
    *,
    title: str,
    lead_id: str,
    stage: str = "",
    last_said: str = "",
    next_action: str = "",
    whatsapp_offered: bool = False,
    who: str = "",
    extra_pairs: list[tuple[str, str]] | None = None,
    after: str = "",
) -> str:
    """Labeled lead card. Empty values are omitted, never invented."""
    blocks = [bold(title)]
    identity = who.strip() or lead_id
    if identity and identity != lead_id:
        blocks.append(esc(identity))
    required = [
        _line("ליד", lead_id, monospace=True),
        _line("שלב", stage),
        _line("מה אמרו", last_said),
        _line("פעולה הבאה", hebrew_next_action(next_action)),
        _line("וואטסאפ הוצע", hebrew_yes_no(whatsapp_offered)),
    ]
    extras = [_line(label, value) for label, value in (extra_pairs or [])]
    body = "\n".join(line for line in [*required, *extras] if line)
    return join_sections(*blocks, body, after)


def card_who(lead_id: str, sales: SalesState) -> str:
    return lead_display(lead_id, sales.headline, sales.display_name)
